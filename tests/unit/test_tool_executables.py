"""Selected polyglot requirements remain inert until runtime acquisition."""
import asyncio
import json
import os
from pathlib import Path
import sys
import threading
from types import MappingProxyType, SimpleNamespace

import pytest

from api_agent.tooling import executables as e
from api_agent.tooling.models import ToolPlan, ToolSelectionError
from test_tool_selection import selection_fixture, plan, write
from test_tool_runtime import admitted, Selected, Plan


def declare(fixture, body, *, implementation='graphql.toml'):
    context, _ = fixture
    target = context.base_root / implementation
    target.write_text(target.read_text().replace('version=1', 'version=1\nexecutables="executables.toml"', 1))
    return write(context.base_root, 'executables.toml', body)


def test_selected_nested_closure_survives_wire_and_case_narrowing(selection_fixture):
    context, _ = selection_fixture
    parent = declare(selection_fixture, 'version=1\nrequires=["nested/toolchain.toml"]\n[tools]\n"core:rust"="1.90.0"\n')
    child = write(context.base_root, 'nested/toolchain.toml', 'version=1\n[tools]\n"core:dotnet"="8.0.425"\n')
    selected = plan(selection_fixture, ('base::tests/test_flow.py::test_primary',))
    restored = ToolPlan.from_wire(json.loads(json.dumps(selected.to_wire()))).for_case('base::tests/test_flow.py::test_primary')

    assert dict(restored.tools[0].executables) == {'core:rust': '1.90.0', 'core:dotnet': '8.0.425'}
    assert {parent, child} <= restored.captured_assets.keys()
    assert restored.tools[0].executable_assets[parent] == parent.read_bytes()
    with pytest.raises(TypeError):
        restored.tools[0].executables['core:rust'] = '1.91.0'


def test_relocated_external_pack_keeps_its_own_toolchain_assets(selection_fixture, tmp_path):
    from dataclasses import replace
    import shutil
    from api_agent.tooling.selection import compose_tools
    context, schema = selection_fixture
    declare(selection_fixture, 'version=1\nrequires=["nested/toolchain.toml"]')
    write(context.base_root, 'nested/toolchain.toml', 'version=1\n[tools]\n"core:rust"="1.90.0"')
    pack = tmp_path.parent / (tmp_path.name + '-pack')
    shutil.copytree(context.base_root, pack)
    relocated = replace(context, pack_root=pack, owner_root=pack, workflow_root='pack',
                        workflow_entry=pack / 'primary_graph.md.html', tool_catalog_path=None)
    selected = compose_tools(relocated, schema, environ={}, dotenv={})
    assert selected.tools[0].executables == {'core:rust': '1.90.0'}
    assert all(path.is_relative_to(pack) for path in selected.tools[0].executable_assets)
    assert selected.tools[0].factory == pack / 'package/factory.py'


def test_inactive_implementation_does_not_read_invalid_manifest(selection_fixture, monkeypatch):
    declare(selection_fixture, 'not valid TOML', implementation='rest.toml')
    monkeypatch.setattr(e, 'prepare_environment', lambda *a, **k: pytest.fail('discovery installed tools'))
    assert not plan(selection_fixture).tools[0].executables
    assert not plan(selection_fixture, ()).tools


@pytest.mark.parametrize('link', ['../base/toolchain.toml', 'absolute', '<resource_pack>/toolchain.toml', 'package:other:pkg/toolchain.toml'])
@pytest.mark.parametrize('nested', [False, True])
def test_executable_links_cannot_cross_owners(tmp_path, link, nested):
    from api_agent.tooling.declarations import Capture
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    base.mkdir()
    pack.mkdir()
    child = write(base, 'toolchain.toml', 'version=1\n[tools]\n"core:rust"="1.90.0"')
    target = str(child) if link == 'absolute' else link
    if nested:
        write(pack, 'executables.toml', 'version=1\nrequires=[' + json.dumps(target) + ']')
        target = 'executables.toml'
    capture = Capture({'base': base, 'pack': pack})
    with pytest.raises(ToolSelectionError, match='owner-relative|escapes its owner'):
        e.capture_executables(capture, pack / 'implementation.toml', target)
    assert child not in capture.bodies


@pytest.mark.parametrize('body, message', [
    ('version=1\nrequires=["executables.toml"]', 'Cyclic'),
    ('version=1\nrequires=["/outside/tool.toml"]', 'owner-relative'),
    ('version=1\nrequires=[true]', 'file path'),
    ('version=2', 'version 1'),
    ('version=true', 'version 1'),
    ('version=1\n[env]\nTOKEN="value"', 'version 1'),
    ('version=1\nrequires="child.toml"', 'version 1'),
    ('version=1\nrequires=[' + ','.join('"child.toml"' for _ in range(65)) + ']', 'version 1'),
    ('version=1\n[tools]\nrust="1.90.0"', 'qualified'),
    ('version=1\n[tools]\n"core:rust"="stable"', 'exact'),
    ('version=1\n[tools]\n"core:rust"="1.90"', 'exact'),
    ('version=1\n[tools]\n"core:rust"="{{exec(command=\'bad\')}}"', 'exact'),
    ('version=1\n[tools]\n"core:rust"={version="1.90.0",postinstall="bad"}', 'exact'),
])
def test_invalid_selected_manifest_rejects_before_acquisition(selection_fixture, body, message):
    declare(selection_fixture, body)
    with pytest.raises(ToolSelectionError, match=message):
        plan(selection_fixture)


def test_recursive_diamond_deduplicates_and_conflicts_reject(selection_fixture):
    context, _ = selection_fixture
    declare(selection_fixture, 'version=1\nrequires=["a.toml","b.toml"]')
    write(context.base_root, 'a.toml', 'version=1\nrequires=["b.toml"]\n[tools]\n"core:rust"="1.90.0"')
    b = write(context.base_root, 'b.toml', 'version=1\n[tools]\n"core:rust"="1.90.0"')
    assert dict(plan(selection_fixture).tools[0].executables) == {'core:rust': '1.90.0'}
    b.write_text(b.read_text().replace('1.90.0', '1.91.0'))
    with pytest.raises(ToolSelectionError, match='Conflicting executable versions for core:rust'):
        plan(selection_fixture)


def test_manifest_count_is_bounded(selection_fixture, monkeypatch):
    context, _ = selection_fixture
    declare(selection_fixture, 'version=1\nrequires=["child.toml"]')
    write(context.base_root, 'child.toml', 'version=1')
    monkeypatch.setattr(e, 'MAX_MANIFESTS', 1)
    with pytest.raises(ToolSelectionError, match='manifest limit'):
        plan(selection_fixture)


def test_union_tool_count_is_bounded(selection_fixture, monkeypatch):
    context, _ = selection_fixture
    declare(selection_fixture, 'version=1\nrequires=["child.toml"]\n[tools]\n"core:rust"="1.90.0"')
    write(context.base_root, 'child.toml', 'version=1\n[tools]\n"core:dotnet"="8.0.425"')
    monkeypatch.setattr(e, 'MAX_TOOLS', 1)
    with pytest.raises(ToolSelectionError, match='tool limit'):
        plan(selection_fixture)


@pytest.fixture
def fake_mise(tmp_path, monkeypatch):
    binary = tmp_path / 'mise'
    log = tmp_path / 'calls.jsonl'
    binary.write_text(f'''#!{sys.executable}
import json, os, sys, time
from pathlib import Path
with open(os.environ['TOOL_MISE_LOG'], 'a') as stream:
    stream.write(json.dumps([sys.argv[1:], Path('mise.toml').read_text(), os.environ.get('MISE_ENV')])+'\\n')
if os.environ.get('TOOL_MISE_WAIT'):
    Path(os.environ['TOOL_MISE_WAIT']).touch()
    time.sleep(10)
if os.environ.get('TOOL_MISE_FAIL'):
    print('synthetic secret', file=sys.stderr)
    sys.exit(1)
if '--json' in sys.argv:
    print(os.environ.get('TOOL_MISE_JSON', '{{"PATH":"/tool/bin:/usr/bin","RUSTUP_TOOLCHAIN":"1.90.0"}}'))
''')
    binary.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path))
    monkeypatch.setenv('TOOL_MISE_LOG', str(log))
    monkeypatch.setenv('MISE_ENV', 'hostile')
    return binary, log


def test_preparation_returns_backend_environment_without_mutating_parent(fake_mise):
    _, log = fake_mise
    before = dict(os.environ)
    overrides = e.prepare_environment({'core:rust': '1.90.0'})
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert overrides == {'PATH': '/tool/bin:/usr/bin', 'RUSTUP_TOOLCHAIN': '1.90.0'}
    assert dict(os.environ) == before
    assert [row[0][-1] for row in calls] == ['install', '--json']
    assert all(row[2] == '' for row in calls)
    assert '"core:rust" = "1.90.0"' in calls[0][1]
    assert 'dotnet.isolated = true' in calls[0][1]
    assert not Path(calls[0][0][1]).exists()
    with pytest.raises(TypeError):
        overrides['PATH'] = '/wrong'


def test_empty_requirements_do_not_need_mise(monkeypatch):
    monkeypatch.setenv('PATH', '')
    assert e.prepare_environment({}) == {}


def test_missing_mise_is_actionable(monkeypatch):
    monkeypatch.setenv('PATH', '')
    with pytest.raises(OSError, match='requires mise on PATH'):
        e.prepare_environment({'core:rust': '1.90.0'})


@pytest.mark.parametrize('raw', ['not json', '[]', '{}', '{"PATH":null}', '{"PATH":"x","BAD-NAME":"value"}', '{"PATH":"x","OTHER":true}', '{"PATH":"x\\u0000"}'])
def test_invalid_environment_is_not_published(fake_mise, monkeypatch, raw):
    monkeypatch.setenv('TOOL_MISE_JSON', raw)
    with pytest.raises(OSError, match='invalid toolchain environment'):
        e.prepare_environment({'core:rust': '1.90.0'})


def test_install_failure_does_not_forward_arbitrary_output(fake_mise, monkeypatch):
    monkeypatch.setenv('TOOL_MISE_FAIL', '1')
    with pytest.raises(OSError, match='host prerequisites') as caught:
        e.prepare_environment({'core:rust': '1.90.0'})
    assert 'synthetic secret' not in str(caught.value)


def test_timeout_drains_installer(fake_mise, monkeypatch, tmp_path):
    marker = tmp_path / 'waiting'
    monkeypatch.setenv('TOOL_MISE_WAIT', str(marker))
    with pytest.raises(TimeoutError, match='timed out'):
        e.prepare_environment({'core:rust': '1.90.0'}, timeout=.3)
    assert marker.exists()


def test_async_preparation_deduplicates_per_tool_not_globally(monkeypatch):
    calls = []
    def prepare(tools, **kwargs):
        calls.append(tools)
        return MappingProxyType({'PATH': '/tools/' + tools['core:rust']})
    monkeypatch.setattr(e, 'prepare_environment', prepare)
    selected = SimpleNamespace(tools=[SimpleNamespace(id=name, executables=tools) for name, tools in
        [('a', {'core:rust': '1.90.0'}), ('b', {'core:rust': '1.90.0'}), ('c', {'core:rust': '1.91.0'}), ('d', {})]])
    result = asyncio.run(e.prepare_environments(selected))
    assert len(calls) == 2
    assert result['a'] is result['b']
    assert result['a']['PATH'] != result['c']['PATH']
    assert 'd' not in result


def test_cancellation_drains_thread_before_return(monkeypatch):
    started, finished = threading.Event(), threading.Event()
    def prepare(tools, *, cancellation, **kwargs):
        started.set()
        assert cancellation.wait(5)
        finished.set()
        raise InterruptedError()
    monkeypatch.setattr(e, 'prepare_environment', prepare)
    async def scenario():
        task = asyncio.create_task(e.prepare_environments(SimpleNamespace(tools=[SimpleNamespace(id='a', executables={'core:rust': '1.90.0'})])))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
    asyncio.run(scenario())


def test_pre_cancelled_and_expired_preparation_do_not_spawn(fake_mise):
    _, log = fake_mise
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(InterruptedError, match='cancelled'):
        e.prepare_environment({'core:rust': '1.90.0'}, cancellation=cancelled)
    with pytest.raises(TimeoutError, match='timed out'):
        e.prepare_environment({'core:rust': '1.90.0'}, timeout=1e-15)
    assert not log.exists()


def test_invalid_mise_executable_has_safe_error(fake_mise):
    binary, _ = fake_mise
    binary.write_text('invalid executable')
    with pytest.raises(OSError, match='could not start'):
        e.prepare_environment({'core:rust': '1.90.0'})


def test_cancellation_during_command_drains_it(fake_mise, monkeypatch, tmp_path):
    marker = tmp_path / 'waiting'
    monkeypatch.setenv('TOOL_MISE_WAIT', str(marker))
    cancelled = threading.Event()
    failures = []
    def prepare():
        try:
            e.prepare_environment({'core:rust': '1.90.0'}, cancellation=cancelled)
        except BaseException as error:
            failures.append(error)
    task = threading.Thread(target=prepare)
    task.start()
    try:
        import time
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert marker.exists()
        cancelled.set()
        task.join(5)
        assert not task.is_alive()
        assert len(failures) == 1 and isinstance(failures[0], InterruptedError)
    finally:
        cancelled.set()
        task.join(5)


def test_oversized_command_output_is_rejected(fake_mise, monkeypatch):
    binary, _ = fake_mise
    binary.write_text(f'#!{sys.executable}\nprint("x" * (1024 * 1024 + 1))\n')
    with pytest.raises(OSError, match='output limit'):
        e.prepare_environment({'core:rust': '1.90.0'})


def test_cancellation_before_environment_publication_rejects(fake_mise, monkeypatch):
    cancelled = threading.Event()
    original = e.json.loads
    def decode(body):
        result = original(body)
        cancelled.set()
        return result
    monkeypatch.setattr(e.json, 'loads', decode)
    with pytest.raises(InterruptedError, match='cancelled'):
        e.prepare_environment({'core:rust': '1.90.0'}, cancellation=cancelled)


def test_symlinked_manifest_is_rejected(selection_fixture):
    context, _ = selection_fixture
    target = declare(selection_fixture, 'version=1')
    target.rename(context.base_root / 'real.toml')
    target.symlink_to('real.toml')
    with pytest.raises(ToolSelectionError, match='Cannot capture'):
        plan(selection_fixture)


def test_runtime_passes_only_each_tools_environment(admitted, monkeypatch):
    from api_agent.api.application import Runtime
    from api_agent.tooling import services
    from dataclasses import replace
    from api_agent.tooling.admission import _identity

    source = dict(admitted.sources)
    source['factory.py'] = source['factory.py'].replace(b'return Owner(local_config)',
        b'return Owner({**local_config, "environment": services["tool_environment"]})')
    captured = replace(admitted, sources=source, identity=_identity(source, admitted.dependencies, admitted.assets))
    selected = Plan((Selected('sample/rust', captured, {'VALUE': 'a'}, executables={'core:rust': '1.90.0'}),
                     Selected('sample/other', captured, {'VALUE': 'b'}, executables={'core:rust': '1.91.0'})))
    monkeypatch.setattr(e, 'prepare_environment', lambda tools, **kwargs: MappingProxyType({'RUSTUP_TOOLCHAIN': tools['core:rust']}))

    async def scenario():
        runtime = Runtime('sample', tool_plan=selected)
        await runtime.astart()
        try:
            assert runtime.tool_owners['sample/rust'].require_tool().config['environment']['RUSTUP_TOOLCHAIN'] == '1.90.0'
            assert runtime.tool_owners['sample/other'].require_tool().config['environment']['RUSTUP_TOOLCHAIN'] == '1.91.0'
        finally:
            await runtime.aclose()
    asyncio.run(scenario())

    with pytest.raises(ValueError, match='environment is not prepared'):
        services.tool_configurations(selected, {})


def test_invalid_tool_services_reject_before_any_installer(admitted, monkeypatch):
    from api_agent.tooling.services import prepare_tool_configurations
    selected = Plan((Selected('sample/rust', admitted, {}, service_bindings={'db': 'sample/db'},
        service_capabilities={'db': 'db@1'}, executables={'core:rust': '1.90.0'}),))
    monkeypatch.setattr(e, 'prepare_environment', lambda *a, **k: pytest.fail('installed before validation'))
    with pytest.raises(ValueError, match='service registry is unavailable'):
        asyncio.run(prepare_tool_configurations(selected, {}))
