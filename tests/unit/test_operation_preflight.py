"""Pack-owned execution checks leave review/collection and demo unchanged."""
import asyncio
from unittest.mock import Mock, patch
import pytest
from textual.widgets import Static
from api_agent.auth.extensions import validate_operation
from test_identity_extensions import pack, ADAPTER
from test_tui import fixture, Identity

TODO = 'TODO: need to automatically start up API when API_ENDPOINT is missing'


def test_optional_execution_hook_and_standalone_remain_compatible(tmp_path):
    validate_operation(pack(tmp_path, identity=False), {})
    validate_operation(pack(tmp_path), {'configured':'yes'})
    context = pack(tmp_path, ADAPTER.replace('    version = 1',
        '    version = 1\n    def validate_operation(self): raise ValueError("pending dependency")'))
    with pytest.raises(ValueError, match='Operation preflight failed; check pack configuration'):
        validate_operation(context, {'configured':'yes'})


@pytest.mark.parametrize('mode', ['run-local','integration','e2e'])
def test_tui_checks_dependency_at_start_not_during_collection(tmp_path, monkeypatch, mode):
    check = Mock(side_effect=ValueError(TODO))
    monkeypatch.setattr('api_agent.auth.extensions.validate_operation', check)
    identity = Identity('base::test.py::test_one','base','test.py','test.py::test_one', ('requires_pack_api',))
    async def scenario():
        app=fixture(tmp_path, mode, collector=lambda *args:[identity])
        async with app.run_test() as pilot:
            await pilot.pause()
            if mode != 'run-local':
                assert app.screen.kind=='tests'
                check.assert_not_called()
                await app.choose('continue')
                if mode=='e2e':
                    assert app.screen.kind=='acceptance'
                    check.assert_not_called()
                    await app.choose('accept')
            await pilot.pause()
            assert app.screen.kind=='unavailable'
            assert TODO in str(app.screen.query_one('#body',Static).content)
            assert app.operation_status==3
            check.assert_called_once_with(app.context, app.session.result().values)
    asyncio.run(scenario())


def test_local_run_without_pack_dependency_reaches_existing_stub(tmp_path):
    async def scenario():
        app=fixture(tmp_path,'run-local')
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.kind=='unavailable'
            assert 'Run local execution is not implemented yet' in str(app.screen.query_one('#body',Static).content)
    asyncio.run(scenario())


@pytest.mark.parametrize('endpoint', [None, '', 'https://api.example/graphql'])
def test_no_stdin_local_run_reports_pack_todo_only_for_missing_endpoint(tmp_path, monkeypatch, capsys, endpoint):
    from pathlib import Path
    from api_agent.runtime.bootstrap import main
    base = Path(__file__).resolve().parents[2]
    source = ADAPTER.replace('    version = 1',
        '    version = 1\n    def validate_operation(self):\n        if not self.endpoint: raise OperationUnavailableError("' + TODO + '")')
    source = 'from api_agent.auth.extensions import OperationUnavailableError\n' + source
    source = source.replace("assert config['configured'] == 'yes'\n    return Adapter()",
        "adapter=Adapter()\n    adapter.endpoint=config.get('API_ENDPOINT')\n    return adapter")
    context = pack(tmp_path, source)
    context.config_path.write_text('version=1\n[fields.API_ENDPOINT]\ntype="str"\n')
    monkeypatch.setattr('api_agent.config.read_dotenv', lambda path: {})
    with patch.dict('os.environ', {}, clear=True):
        args = ['--no-std-in', '--tui-mode=run-local']
        if endpoint is not None:
            args.append('--api-endpoint=' + endpoint)
        assert main(args, base_root=base, resource_pack_dir=context.pack_root, _worktrees_verified=True) == 3
    output = capsys.readouterr().err
    if endpoint:
        assert TODO not in output and 'Requested operation is not implemented yet' in output
    else:
        assert output.strip() == TODO


@pytest.mark.parametrize('mode', ['integration', 'e2e'])
def test_collection_does_not_run_pack_execution_hook(tmp_path, monkeypatch, mode):
    from pathlib import Path
    from api_agent.runtime.bootstrap import main
    base = Path(__file__).resolve().parents[2]
    context = pack(tmp_path, identity=False)
    check = Mock(side_effect=AssertionError('Collection must not check execution prerequisites'))
    monkeypatch.setattr('api_agent.auth.extensions.validate_operation', check)
    monkeypatch.setattr('api_agent.config.read_dotenv', lambda path: {})
    monkeypatch.setattr('api_agent.runtime.dependencies.ensure_environment', lambda *a, **k: None)
    monkeypatch.setattr('api_agent.testing.collection.collect_tests', lambda *a, **k: ())
    with patch.dict('os.environ', {}, clear=True):
        assert main(['--no-std-in', '--tui-mode=' + mode, '--collect-only'],
                    base_root=base, resource_pack_dir=context.pack_root, _worktrees_verified=True) == 0
    check.assert_not_called()


@pytest.mark.parametrize('interface', ['tui', 'no-stdin'])
def test_pack_validator_failure_never_displays_secret(tmp_path, monkeypatch, capsys, interface):
    from pathlib import Path
    from api_agent.runtime.bootstrap import main
    secret='synthetic-secret-marker'
    source=ADAPTER.replace('    version = 1',
        '    version = 1\n    def validate_operation(self): raise ValueError("backend failed with " + self.secret)')
    source=source.replace("assert config['configured'] == 'yes'\n    return Adapter()",
        "adapter=Adapter()\n    adapter.secret=config['EXTENSION_SECRET']\n    return adapter")
    context=pack(tmp_path,source)
    message='Operation preflight failed; check pack configuration'
    if interface=='tui':
        async def scenario():
            app=fixture(tmp_path,'run-local',fields={'EXTENSION_SECRET':{'type':'str','default':secret}})
            app.context=app.session.context=context
            async with app.run_test() as pilot:
                await pilot.pause()
                assert app.screen.kind=='unavailable'
                content=str(app.screen.query_one('#body',Static).content)
                assert message in content
                assert secret not in content and 'backend failed' not in content
                assert app.operation_status==3
        asyncio.run(scenario())
    else:
        context.config_path.write_text('version=1\n[fields.EXTENSION_SECRET]\ntype="str"\ndefault="'+secret+'"\n')
        monkeypatch.setattr('api_agent.config.read_dotenv',lambda path:{})
        with patch.dict('os.environ',{},clear=True):
            assert main(['--no-std-in','--tui-mode=run-local'],
                base_root=Path(__file__).resolve().parents[2],resource_pack_dir=context.pack_root,
                _worktrees_verified=True)==3
        output=capsys.readouterr().err
        assert output.strip()==message
        assert secret not in output and 'backend failed' not in output


def test_provider_only_integration_start_does_not_initialize_pack_identity(tmp_path, monkeypatch):
    from types import SimpleNamespace
    check = Mock(side_effect=AssertionError('Unrelated pack API was initialized'))
    monkeypatch.setattr('api_agent.auth.extensions.validate_operation', check)
    identity = Identity('base::test.py::test_one', 'base', 'test.py', 'test.py::test_one')
    async def scenario():
        app = fixture(tmp_path, 'integration', collector=lambda *args: [identity])
        execution = SimpleNamespace(tests=(), exit_code=0, cancelled=False)
        runner = Mock(return_value=SimpleNamespace(execution=execution, exit_code=0, persisted=True))
        app.integration_runner = runner
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.choose('continue')
            await app.workers.wait_for_complete()
            assert runner.call_count == 1
            assert app.operation_status == 0
            check.assert_not_called()
    asyncio.run(scenario())


def test_requirement_plan_is_selection_local_and_shared(tmp_path, monkeypatch):
    from api_agent.testing.requirements import plan_integration_requirements
    check = Mock()
    monkeypatch.setattr('api_agent.auth.extensions.validate_operation', check)
    api = Identity('base::test.py::test_api', 'base', 'test.py', 'test.py::test_api', ('requires_pack_api',))
    llm = Identity('base::test.py::test_llm', 'base', 'test.py', 'test.py::test_llm')
    ctx, config = object(), {'setting': 'value'}
    independent = plan_integration_requirements([llm])
    assert not independent.requires_pack_api
    independent.validate(ctx, config)
    check.assert_not_called()
    dependent = plan_integration_requirements([api, llm])
    assert dependent.requires_pack_api
    dependent.validate(ctx, config)
    check.assert_called_once_with(ctx, config)
