"""The bundled full test selection keeps per-case inputs and outputs isolated."""
import json
from pathlib import Path
from types import SimpleNamespace
import tomllib

import pytest

from api_agent.config import compose_schema, load_declarations, load_shared_schema, resolve_config
from api_agent.providers.loading import bootstrap_schema
from api_agent.runtime.resource_pack import load_launch_context
from api_agent.tooling.models import OperationSelection, ToolPlan
from api_agent.tooling.selection import compose_tools
from api_agent.tooling.services import bind_tool_outputs


@pytest.mark.parametrize('with_provider', [False, True])
def test_full_bundled_selection_reuses_inputs_without_merging_cases(with_provider):
    root = Path(__file__).resolve().parents[2]
    context = load_launch_context(root, root / 'demo')
    shared = load_shared_schema(root)
    schema = compose_schema(shared, load_declarations(context.config_path,
        base_schema=shared, source='pack_default'), overrides=context.defaults)
    environment = ({'API_PROVIDER': str(root / 'src/api_agent/api_providers/graphql_mcp/factory.py') + ':factory'}
                   if with_provider else {})
    schema = bootstrap_schema(schema, (), environment, {}, owner_root=context.owner_root,
                              configuration_paths=context.provider_config_paths)
    cases = tuple(tomllib.loads(context.tool_catalog_path.read_text())['test_cases'])

    plan = compose_tools(context, schema, selection=OperationSelection('integration', cases),
                         environ=environment, dotenv={})
    result = resolve_config(plan.schema, environ={}, dotenv={},
                            overrides={'TUI_MODE': 'integration', 'API_ENDPOINT': '', 'API_HEADERS': {}})
    restored = ToolPlan.from_wire(json.loads(json.dumps(plan.bind_values(result).to_wire())))
    application_case = next(case for case in cases if 'test_selected_graph_relaxes_preference' in case)
    shared_case = next(case for case in cases if 'test_shared_primary' in case)
    application = restored.for_case(application_case)
    fixture = restored.for_case(shared_case)

    assert [tool.id for tool in application.tools] == ['demo/primary']
    assert {tool.id for tool in fixture.tools} == {'fixture/primary', 'fixture/shared-secondary'}
    assert application.tools[0].bindings['ENDPOINT'] == fixture.tools[0].bindings['ENDPOINT'] == 'API_ENDPOINT'
    assert application.tools[0].pending['ENDPOINT']['output'] == 'demo/api/graphql'
    assert fixture.tools[0].pending['ENDPOINT']['output'] == 'fixture/primary/http'
    assert {service.owner for service in application.infrastructure_contributions} == {'demo'}
    assert {service.owner for service in fixture.infrastructure_contributions} == {'fixture'}
    assert not (set(application.bindings.values()) & set(fixture.bindings.values()))

    outputs = SimpleNamespace(endpoints={
        'demo/api': [SimpleNamespace(name='graphql', host='http://127.0.0.1:18001')],
        'fixture/primary': [SimpleNamespace(name='http', host='http://127.0.0.1:18002')]})
    application_ready = bind_tool_outputs(application, outputs)
    fixture_ready = bind_tool_outputs(fixture, outputs)

    assert application_ready.tools[0].local_values['ENDPOINT'] == 'http://127.0.0.1:18001/graphql'
    assert {tool.local_values['ENDPOINT'] for tool in fixture_ready.tools} == {'http://127.0.0.1:18002/graphql'}
    assert application_ready.tools[0].configured_values['ENDPOINT'] == ''
    assert fixture_ready.tools[0].configured_values['ENDPOINT'] == ''
    assert 'ENDPOINT' not in fixture_ready.tools[1].configured_values
    assert fixture_ready.tools[1].derived_from['ENDPOINT'] == 'fixture/primary'
