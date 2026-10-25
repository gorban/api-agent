"""Configuration, infrastructure and borrowed services remain independent axes."""
from types import MappingProxyType

import pytest

from api_agent.api.application import Runtime
from api_agent.config.declarations import ConfigSchema
from api_agent.config.resolve import resolve_config
from api_agent.runtime.resource_pack import LaunchContext
from api_agent.tooling.capabilities import ServiceBinding, ServiceRegistry
from api_agent.tooling.selection import compose_tools
from api_agent.tooling.services import selected_contributions
from api_agent.workflows.definition import load_native_catalog
from api_agent.workflows.execution import NodeFunction, Workflow


SOURCE = '''from api_agent.providers.tools import ToolSpec, ToolResult
class Owner:
    version = 1
    def __init__(self, config, services): self.config, self.services = config, services
    async def describe(self):
        return ToolSpec('read', 'Read a synthetic label', {'type':'object','properties':{},'additionalProperties':False}, {'type':'object'}, 'a'*64)
    async def validate(self, prepared): pass
    async def invoke(self, prepared):
        value = {'label': self.config.get('LABEL', 'without-config')}
        self.services['tool_services']['sink'].put('result', value)
        return ToolResult('success', value)
    async def aclose(self): self.services['events'].append('tool close')
def create_tool(config, services): return Owner(config, services)
'''
NATIVE = '''async def execute(inputs, context):
    gate = context.services['tools'].gate(scope_kind='node',scope='read',port='reader',invocation_id='shape-call')
    prepared = await gate.prepare({})
    verified = await gate.verify(prepared, relevance=lambda candidate: {'relevant':True,'grounded':True})
    return (await gate.execute(verified)).data
'''


class ResultStore:
    def __init__(self, events):
        self.values, self.events = {}, events

    async def aclose(self):
        self.events.append('store close')


class WriteView:
    def __init__(self, values):
        self._values = values

    def put(self, key, value):
        self._values[key] = value


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def shape_plan(root, with_config, with_prerequisite):
    write(root, 'provider/factory.py', SOURCE)
    write(root, 'native.py', NATIVE)
    write(root, 'empty.json', '{"type":"object","properties":{},"additionalProperties":false}')
    write(root, 'output.json', '{"type":"object","properties":{"label":{"type":"string"}},"required":["label"],"additionalProperties":false}')
    write(root, 'node.md.html', '# Node v2: read\n\n## Native\n\n| Registry | Kind | Source |\n| --- | --- | --- |\n'
          '| execute | deterministic | [source](native.py) |\n\n## Functions\n\n'
          '| ID | Registry | Input | Output |\n| --- | --- | --- | --- |\n'
          '| read | execute | [input](empty.json) | [output](output.json) |\n\n## Tools\n\n'
          '| Port | Instance |\n| --- | --- |\n| reader | [instance](instance.toml) |\n')
    entry = write(root, 'flow.md.html', '# Workflow v2\n\n## Graph ID\n\n| ID |\n| --- |\n| shape |\n\n'
                  '## Flow\n\n1.  [node read](node.md.html)\n')
    instance = 'version=1\nid="fixture/shape"\ninterface="verified-read@1"\nimplementation="implementation.toml"\n'
    if with_prerequisite:
        instance += 'prerequisites=["fixture/store"]\n[infrastructure]\nowner="fixture"\nroot="."\ndeclarations=["infrastructure.toml"]\n'
    instance += '[services]\nsink="fixture/results"\n'
    if with_config:
        instance += '[bindings]\nLABEL="SHAPE_LABEL"\n'
    write(root, 'instance.toml', instance)
    implementation = 'version=1\ninterface="verified-read@1"\nfactory="provider/factory.py:create_tool"\n[services]\nsink="result-write@1"\n'
    if with_config:
        implementation += '[inputs.LABEL]\ntype="str"\ndefault="configured-label"\n'
    write(root, 'implementation.toml', implementation)
    write(root, 'infrastructure.toml', 'version=1\nowner="fixture"\n[[services]]\nid="store"\ncompose="store.json"\nreadiness={kind="health"}\n')
    write(root, 'store.json', '{"image":"example/store@sha256:' + 'a' * 64 + '"}')
    context = LaunchContext(root, root, root, 'fixture', 'fixture', root / 'config.toml',
                            MappingProxyType({}), MappingProxyType({}), workflow_entry=entry)
    plan = compose_tools(context, ConfigSchema({}), environ={})
    return plan.bind_values(resolve_config(plan.schema, environ={}))


@pytest.mark.asyncio
@pytest.mark.parametrize('with_config,with_prerequisite', [(False, False), (True, False),
                                                        (False, True), (True, True)])
async def test_four_shapes_execute_true_workflow_with_borrowed_result_view(tmp_path, with_config, with_prerequisite):
    plan = shape_plan(tmp_path, with_config, with_prerequisite)
    assert bool(plan.schema.fields) is with_config
    assert bool(selected_contributions(plan)) is with_prerequisite
    assert all('ENDPOINT' not in field and 'S3' not in field for field in plan.schema.fields)
    events = []
    store = ResultStore(events)
    view = WriteView(store.values)
    registry = ServiceRegistry({'fixture/results': ServiceBinding(store, {'result-write@1': view})})
    runtime = Runtime('fixture', tool_plan=plan, services={'service_registry': registry, 'events': events})
    await runtime.astart()
    try:
        owner = runtime.tool_owners['fixture/shape'].require_tool()
        assert 'service_registry' not in owner.services
        assert owner.services['tool_services'] == {'sink': view}
        definition, = plan.definitions
        source = next(step.source for step in definition.layout.steps if step.id == 'read')
        native = load_native_catalog(definition, source)
        graph = Workflow(definition, {'execute': NodeFunction(native['execute'])},
                         services={'tools': runtime.tools_for(definition)})
        expected = {'label': 'configured-label' if with_config else 'without-config'}
        assert (await graph.arun({}))['read'] == expected
        assert store.values == {'result': expected}
        assert not any(hasattr(view, name) for name in ('close', 'aclose', 'dispose'))
        assert not events
    finally:
        await runtime.aclose()
    assert events == ['tool close', 'store close']
