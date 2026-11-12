"""Production admits the compiled plan's declared tools through the ordinary tool admission."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from artifact_acceptance_support import build_wheel, install
from artifact_unit_support import QUALIFICATION, copy_tree, isolated_imports  # noqa: F401
from api_agent.runtime import production
from api_agent.workflows import artifact
from api_agent.workflows.artifact_compile import compile_plan

ENDPOINT = 'http://127.0.0.1:9/graphql'
NATIVE = '''from typing import TypedDict

class State(TypedDict):
    messages: list
    mode: str
    attempts: int
    feedback: str
    trail: list
    outcome: str

async def intake(state, context):
    prepared = await context.tools['compiled'].prepare({})
    context.services['grant'] = await context.tools['compiled'].verify(
        prepared, relevance=lambda _: {'relevant': True, 'grounded': True})
    return {'trail': [*state['trail'], 'intake'], 'attempts': 0, 'feedback': '', 'outcome': ''}

async def work(state, context):
    return {'trail': [*state['trail'], 'work'], 'attempts': 1, 'feedback': ''}

def route_work(state, context):
    return 'finish'

async def finish(state, context):
    await context.tools['compiled'].execute(context.services['grant'])
    return {'trail': [*state['trail'], 'finish'], 'outcome': 'done'}
'''
INSTANCE = '''version = 1
id = "sample/compiled"
interface = "verified-read@1"
implementation = "package:aq-bindings:aq_bindings/tool_impl.toml"

[bindings]
ENDPOINT = "SAMPLE_ENDPOINT"
HEADERS = "SAMPLE_HEADERS"

[defaults]
ENDPOINT = ""

[local]
slot = "ENDPOINT"
output = "sample/api/graphql"
path = "/graphql"
declaration = "missing/infrastructure.toml"

[infrastructure]
owner = "sample"
root = "."
declarations = ["missing/infrastructure.toml"]
'''
IMPLEMENTATION = b'''version = 1
interface = "verified-read@1"
factory = "factory.py:create_tool"

[inputs.ENDPOINT]
type = "str"
required = true
nonempty = true

[inputs.HEADERS]
type = "jsonobject"
default = {}
'''
FACTORY = b'''from hashlib import sha256
from api_agent.providers.tools import ToolResult, ToolSpec

class Owner:
    version = 1
    def __init__(self, config):
        self.config = config
    async def describe(self):
        return ToolSpec("compiled", "Compiled fixture", {"type": "object"}, {"type": "object"},
                        sha256(b"compiled-tool").hexdigest())
    async def validate(self, prepared): pass
    async def invoke(self, prepared): return ToolResult("success", {})
    async def aclose(self): pass

def create_tool(config, services): return Owner(config)
'''


def declared_plan(directory, instance, implementation=None):
    """Compile a graph whose tool instance names an installed implementation; return plan and bindings wheel."""
    root = copy_tree(QUALIFICATION, directory / 'tree')
    (root / 'instance.toml').write_text(instance)
    for node, operations in (('intake', 'prepare, verify'), ('finish', 'execute')):
        path = root / f'{node}.md.html'
        path.write_text(path.read_text() + '\n## Tools\n\n| Port | Instance | Operations |\n'
                        f'| --- | --- | --- |\n| compiled | [instance](instance.toml) | {operations} |\n')
    native = root / 'src/aq_bindings'
    (native / 'native.py').write_text(NATIVE)
    wheel = build_wheel(directory, 'aq-bindings', {
        'aq_bindings/__init__.py': (native / '__init__.py').read_bytes(),
        'aq_bindings/native.py': NATIVE.encode(), **(implementation or {})})
    plan = directory / 'plan.json'
    plan.write_bytes(compile_plan(root / 'flow.md.html', base_root=root, pack_root=root, wheels=[wheel]))
    return plan, wheel


@pytest.fixture
def declared(tmp_path, monkeypatch):
    """A compiled plan whose declared tool names an installed implementation, plus its environment."""
    plan, wheel = declared_plan(tmp_path, INSTANCE, {
        'aq_bindings/tool_impl.toml': IMPLEMENTATION, 'aq_bindings/factory.py': FACTORY})
    monkeypatch.syspath_prepend(str(install(wheel, tmp_path / 'site')))
    provider = tmp_path / 'llm.py'
    provider.write_text('def create(config, services): return None\n')
    environment = {
        'API_AGENT_PRODUCTION_PLAN': str(plan), 'API_AGENT_OWNER_ROOT': str(tmp_path),
        'API_AGENT_LLM_PROVIDER': f'{provider}:create', 'API_AGENT_CHAT_ORIGIN': 'http://localhost:3000',
        'API_AGENT_SESSION_NONCE': 'n' * 32, 'SAMPLE_ENDPOINT': ENDPOINT,
        'SAMPLE_HEADERS': '{"Authorization": "Bearer synthetic"}',
    }
    return SimpleNamespace(plan=plan, environment=environment, tmp_path=tmp_path)


def _start(environment):
    settings = production.settings_from_environment(environment)
    definition = artifact.load_artifact(settings.plan.read_bytes())
    return settings, definition, production.compose_tool_configuration(settings, definition)


def test_declared_tool_receives_its_bindings_from_the_production_configuration(declared):
    settings, definition, config = _start(declared.environment)

    tool, = config.tool_plan.tools
    assert tool.local_values['ENDPOINT'] == ENDPOINT
    assert tool.local_values['HEADERS'] == {'Authorization': 'Bearer synthetic'}
    assert tool.bindings == {'ENDPOINT': 'SAMPLE_ENDPOINT', 'HEADERS': 'SAMPLE_HEADERS'}
    assert config.values['SAMPLE_ENDPOINT'] == ENDPOINT
    assert config.fields['SAMPLE_ENDPOINT'].source == 'environment'
    assert config.values['NO_STD_IN'] is True and config.values['RUN_UI'] is False
    assert config.tool_plan.definitions[0].definition_id == definition.definition_id


def test_production_ignores_the_demonstration_local_fallback(declared):
    """A blank binding is a configuration error, never a request for a local service."""
    del declared.environment['SAMPLE_ENDPOINT']

    with pytest.raises(production.ProductionConfigurationError, match='SAMPLE_ENDPOINT'):
        _start(declared.environment)


def test_declared_tool_is_admitted_for_the_runtime(declared):
    from api_agent.api.application import Runtime

    _, definition, config = _start(declared.environment)

    async def acquire():
        runtime = Runtime('fixture', tool_plan=config.tool_plan, services={})
        await runtime.astart()
        try:
            owner = runtime.tool_owners['sample/compiled']
            assert runtime.tools_for(definition).for_node('intake')
            return owner
        finally:
            await runtime.aclose()

    assert asyncio.run(acquire()) is not None


def test_start_passes_the_composed_configuration_to_the_chat_runtime(declared, monkeypatch):
    settings = production.settings_from_environment(declared.environment)
    compose = Mock(return_value=SimpleNamespace(runtime=SimpleNamespace(
        astart=AsyncMock(), aclose=AsyncMock(), started=True, ready=True)))
    applications = Mock(return_value=SimpleNamespace())
    monkeypatch.setattr(production, 'compose_chat_runtime', compose)
    monkeypatch.setattr(production, 'selected_application', applications)
    monkeypatch.setattr(production, 'create_application', Mock(return_value=AsyncMock()))

    asyncio.run(production.ProductionApplication(settings, llm_factory=Mock())._start())

    config = compose.call_args.args[1]
    assert config.tool_plan.tools[0].local_values['ENDPOINT'] == ENDPOINT
    assert applications.call_args.kwargs['config'] == config.values


def test_a_plan_without_tools_keeps_the_base_configuration(tmp_path, monkeypatch):
    settings = production.ProductionSettings(
        plan=tmp_path / 'selected.json', pack_id='demo', owner_root=tmp_path,
        listen_host='127.0.0.1', listen_port=8080, callback_origin='https://localhost:9443',
        chat_origin='http://localhost:3000', session_nonce='n' * 32,
        values={'LLM_PROVIDER': 'test:provider'}, shutdown_timeout=1)

    assert production.compose_tool_configuration(settings, SimpleNamespace(tool_usages=())) is None


def test_start_failure_names_the_missing_configuration(declared, caplog):
    del declared.environment['SAMPLE_ENDPOINT']
    settings = production.settings_from_environment(declared.environment)

    with caplog.at_level('ERROR', logger='api_agent.runtime.production'):
        sent = []
        events = iter(({'type': 'lifespan.startup'},))

        async def receive():
            return next(events)

        async def send(message):
            sent.append(message)

        asyncio.run(production.ProductionApplication(settings)({'type': 'lifespan'}, receive, send))

    assert sent == [{'type': 'lifespan.startup.failed', 'message': 'runtime_start_failed'}]
    assert 'SAMPLE_ENDPOINT' in caplog.text


def test_the_separate_api_provider_selection_is_retired(declared):
    declared.environment['API_AGENT_API_PROVIDER'] = '/nonexistent/api.py:create'
    declared.environment['API_PROVIDER'] = '/nonexistent/api.py:create'

    settings = production.settings_from_environment(declared.environment)

    assert settings.values['API_PROVIDER'] == ''


def test_an_inadmissible_tool_declaration_is_a_configuration_error(declared, tmp_path, monkeypatch):
    (tmp_path / 'broken').mkdir()
    plan, wheel = declared_plan(tmp_path / 'broken', INSTANCE.replace('tool_impl.toml', 'absent.toml'), {
        'aq_bindings/tool_impl.toml': IMPLEMENTATION, 'aq_bindings/factory.py': FACTORY})
    monkeypatch.syspath_prepend(str(install(wheel, tmp_path / 'broken-site')))
    declared.environment['API_AGENT_PRODUCTION_PLAN'] = str(plan)

    with pytest.raises(production.ProductionConfigurationError, match='invalid production tool declaration'):
        _start(declared.environment)
