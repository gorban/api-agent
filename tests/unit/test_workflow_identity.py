"""Scoped diagnostic identity causal checks over admitted synthetic graphs."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import shutil

import pytest

from api_agent.workflows.definition import load_definition
from api_agent.workflows.document_format import wrap_document
from api_agent.workflows.identity import declaration_identity, workflow_identity, validate_identity
from api_agent.workflows.journal import WorkflowJournal, validate_journal
from api_agent.workflows.observations import WorkflowObservationFactory
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from test_workflow_inline_declarations import inline_graph, stateful_graph, local_diagram
from api_agent.workflows.stateful import load_stateful_definition


def admit(entry, stateful=False):
    loader = load_stateful_definition if stateful else load_definition
    return loader(entry, base_root=entry.parent, pack_root=entry.parent)


def budgets(definition):
    return workflow_identity(definition, timeouts={}, component_passes={'group': 1})


def test_presentation_line_offsets_relocation_and_no_refresh(inline_graph, tmp_path, monkeypatch):
    entry, component = inline_graph
    original = admit(entry)
    expected = budgets(original)
    component.write_bytes(wrap_document(component.read_bytes().replace(b'## Flow', b'\n\n## Flow'), root=False,
        asset_prefix='.private_resources/viewer/', generated=local_diagram()))
    presented = admit(entry)
    assert presented.definition_id != original.definition_id
    assert budgets(presented) == expected
    relocated = tmp_path / 'elsewhere'
    relocated.mkdir()
    for path in tuple(tmp_path.iterdir()):
        if path.is_file():
            shutil.copyfile(path, relocated / path.name)
    moved = admit(relocated / entry.name)
    assert budgets(moved) == expected
    monkeypatch.setattr(Path, 'read_bytes', lambda *_: pytest.fail('hash refreshed source'))
    assert budgets(original) == expected


@pytest.mark.parametrize('filename,old,new', [
    ('native.py', 'return', '# changed\n    return'),
    ('input.json', 'object', 'object","description":"changed'),
    ('group.md.html', 'Hello.', 'Changed prompt.'),
    ('output.json', 'object', 'object","description":"changed'),
])
def test_exact_bound_resource_bytes_affect_declaration(inline_graph, filename, old, new):
    entry, _ = inline_graph
    expected = budgets(admit(entry))
    path = entry.parent / filename
    path.write_text(path.read_text().replace(old, new))
    actual = budgets(admit(entry))
    assert actual['declaration_sha256'] != expected['declaration_sha256']
    assert actual['settings_sha256'] != expected['settings_sha256']


def add_timeout(entry):
    (entry.parent / 'settings.toml').write_text('version = 1\n[fields.BUDGET]\ntype = "float"\ndefault = 3.0\n[fields.UNUSED]\ntype = "int"\ndefault = 1\n')
    entry.write_text(entry.read_text() + '\n## Timeouts\n\n| Owner | Source | Key |\n| --- | --- | --- |\n| GRAPH | [config](settings.toml) | BUDGET |\n')


def test_selected_config_and_effective_settings_have_separate_scope(inline_graph):
    entry, _ = inline_graph
    add_timeout(entry)
    first = admit(entry)
    expected = workflow_identity(first, timeouts={'GRAPH': 3.0}, component_passes={'group': 1})
    config = entry.parent / 'settings.toml'
    config.write_text(config.read_text().replace('default = 1', 'default = 2'))
    second = admit(entry)
    assert first.definition_id != second.definition_id
    assert workflow_identity(second, timeouts={'GRAPH': 3.0}, component_passes={'group': 1}) == expected
    effective = workflow_identity(second, timeouts={'GRAPH': 4}, component_passes={'group': 1})
    assert effective['declaration_sha256'] == expected['declaration_sha256']
    assert effective['settings_sha256'] != expected['settings_sha256']
    config.write_text(config.read_text().replace('default = 3.0', 'default = 4.0'))
    assert declaration_identity(admit(entry)) != expected['declaration_sha256']


@pytest.mark.parametrize('timeouts,passes', [({}, {}), ({'extra': 1}, {'group': 1}),
    ([], {'group': 1}), ({}, []), ({}, {'group': True}), ({}, {'group': 0}),
    ({}, {'group': 100}), ({}, {'group': 2})])
def test_invalid_budget_maps_reject(inline_graph, timeouts, passes):
    entry, _ = inline_graph
    with pytest.raises(ValueError):
        workflow_identity(admit(entry), timeouts=timeouts, component_passes=passes)


@pytest.mark.parametrize('value', [True, 0, -1, float('nan'), float('inf'), '3'])
def test_invalid_timeout_values_reject(inline_graph, value):
    entry, _ = inline_graph
    add_timeout(entry)
    with pytest.raises(ValueError, match='timeout'):
        workflow_identity(admit(entry), timeouts={'GRAPH': value}, component_passes={'group': 1})


def test_optional_timeout_and_guarded_pass_budget(inline_graph):
    entry, _ = inline_graph
    add_timeout(entry)
    definition = admit(entry)
    rows = [dict(definition.layout.tables['Components'][0], Guard='inspect',
                 max_passes_key='BUDGET', max_passes_link=entry.parent / 'settings.toml')]
    definition = replace(definition, layout=replace(definition.layout,
        tables={**definition.layout.tables, 'Components': rows}))
    first = workflow_identity(definition, timeouts={'GRAPH': None}, component_passes={'group': 2})
    assert first != workflow_identity(definition, timeouts={'GRAPH': 1.0}, component_passes={'group': 3})


def test_stateful_cycle_and_opaque_configuration(tmp_path):
    entry, _ = stateful_graph(tmp_path)
    add_timeout(entry)
    definition = admit(entry, True)
    first = workflow_identity(definition, timeouts={'GRAPH': 3.0}, component_passes={})
    loop = replace(definition, edges=({'From': 'inspect', 'To': 'inspect', 'Router': '-', 'Source': None},))
    assert declaration_identity(loop) != first['declaration_sha256']
    config = tmp_path / 'settings.toml'
    config.write_text(config.read_text().replace('default = 1', 'default = 2'))
    assert declaration_identity(admit(entry, True)) == first['declaration_sha256']
    entry.write_text(entry.read_text() + '\n## Resources\n\n| ID | Kind | Source |\n| --- | --- | --- |\n| settings | configuration | [config](settings.toml) |\n')
    opaque = declaration_identity(admit(entry, True))
    config.write_text(config.read_text().replace('default = 2', 'default = 5'))
    assert declaration_identity(admit(entry, True)) != opaque


@pytest.mark.parametrize('value', [None, {}, {'version': True, 'declaration_sha256': 'a'*64, 'settings_sha256': 'b'*64},
    {'version': 2, 'declaration_sha256': 'a'*64, 'settings_sha256': 'b'*64},
    {'version': 1, 'declaration_sha256': 'A'*64, 'settings_sha256': 'b'*64},
    {'version': 1, 'declaration_sha256': 1, 'settings_sha256': 'b'*64},
    {'version': 1, 'declaration_sha256': 'a'*64, 'settings_sha256': 'b'*63}])
def test_descriptor_shape_rejects(value):
    with pytest.raises(ValueError, match='semantic identity'):
        validate_identity(value)


def resign(packet):
    packet['sha256'] = sha256(json.dumps({k:v for k,v in packet.items() if k != 'sha256'},
        sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    return packet


def test_journal_diagnostic_is_integrity_bound_and_independent_expectation_optional(inline_graph):
    entry, _ = inline_graph
    definition = admit(entry)
    identity = budgets(definition)
    journal = WorkflowJournal(definition, semantic_identity=identity)
    identity['version'] = 2
    journal.observe(dict(event='scope_start', scope_id='graph', kind='graph', owner='GRAPH', started_ms=1))
    journal.semantic_identity['settings_sha256'] = '0'*64
    with pytest.raises(AttributeError):
        journal.semantic_identity = identity
    packet = journal.finalize()
    expected = budgets(definition)
    validate_journal(packet, expected_definition=definition, expected_identity=expected)
    packet['semantic_identity']['settings_sha256'] = '0'*64
    with pytest.raises(ValueError, match='digest'):
        validate_journal(packet)
    resign(packet)
    validate_journal(packet)
    with pytest.raises(ValueError, match='independently expected'):
        validate_journal(packet, expected_identity=expected)
    packet['semantic_identity']['version'] = True
    with pytest.raises(ValueError, match='semantic identity'):
        validate_journal(resign(packet))
    legacy = WorkflowJournal(definition).finalize()
    validate_journal(legacy)
    with pytest.raises(ValueError, match='independently expected'):
        validate_journal(legacy, expected_identity=expected)


def test_factory_retains_original_identity_and_rejects_relabel(inline_graph):
    entry, _ = inline_graph
    definition = admit(entry)
    identity = budgets(definition)
    factory = WorkflowObservationFactory({}, boundary=EmissionBoundary(ContentPolicy(False)))
    observed = factory.create(definition, semantic_identity=identity)
    assert observed.semantic_identity == identity
    retained = WorkflowJournal(definition, semantic_identity=identity)
    assert factory.create(definition, journal=retained).semantic_identity == identity
    explicit = WorkflowJournal(definition, semantic_identity=identity)
    assert factory.create(definition, journal=explicit, semantic_identity=identity).semantic_identity == identity
    with pytest.raises(ValueError, match='conflicts'):
        factory.create(definition, journal=observed.journal,
            semantic_identity=dict(identity, settings_sha256='0'*64))


def test_durable_v2_preserves_top_level_descriptor_and_legacy_digest(inline_graph, tmp_path):
    from api_agent.testing.payloads import PayloadStore, hydrate_journal
    from test_payloads import recorder
    entry, _ = inline_graph
    definition = admit(entry)
    journal = WorkflowJournal(definition, semantic_identity=budgets(definition),
                              log_content=True, content_policy=ContentPolicy(True))
    journal.observe(dict(event='scope_start', scope_id='scope', kind='graph', owner='GRAPH', started_ms=1))
    journal.observe(dict(event='edge', source='START', target='group', owner='GRAPH', implicit=True))
    journal.observe(dict(event='scope_start', scope_id='component', parent_id='scope', kind='component', owner='group', started_ms=1))
    journal.observe(dict(event='edge', source='START', target='inspect', owner='group', implicit=True))
    journal.observe(dict(event='scope_start', scope_id='visit', parent_id='component', kind='node', owner='inspect', started_ms=1))
    journal.capture_json(visit_id='visit', direction='input', values={'text': 'synthetic'*100}, schema={'type':'object'})
    packet = journal.finalize()
    store = PayloadStore(recorder(tmp_path / 'recording'), 'test')
    durable = store.materialize_journal(packet)
    assert durable['version'] == 2
    assert durable['semantic_identity'] == packet['semantic_identity']
    assert any('payload' in row for row in durable['events'])
    assert hydrate_journal(durable, lambda ref: store._values[ref['source']]) == packet


def test_missing_resource_is_not_replaced_with_machine_path(inline_graph):
    entry, _ = inline_graph
    definition = admit(entry)
    with pytest.raises(ValueError, match='Missing or ambiguous'):
        declaration_identity(replace(definition, resources=()))


def test_admitted_string_replacement_and_literal_guard_limit(inline_graph):
    from api_agent.workflows.definition import Replacement
    entry, _ = inline_graph
    definition = load_definition(entry, base_root=entry.parent, pack_root=entry.parent,
        replacements=(Replacement('group', 'group.md.html', 'base'),))
    assert budgets(definition)
    literal_guard = dict(definition.layout.tables['Components'][0], Guard='inspect')
    definition = replace(definition, layout=replace(definition.layout,
        tables={**definition.layout.tables, 'Components': [literal_guard]}))
    with pytest.raises(ValueError, match='pass budget'):
        workflow_identity(definition, timeouts={}, component_passes={'group': 2})


def test_unknown_definition_type_rejects(inline_graph):
    from types import SimpleNamespace
    entry, _ = inline_graph
    with pytest.raises(TypeError, match='admitted workflow'):
        declaration_identity(SimpleNamespace(resources=admit(entry).resources))


@pytest.mark.parametrize('stateful', [False, True])
@pytest.mark.parametrize('supplied', [False, True])
def test_executors_record_actual_validated_maps_and_reject_mismatch(
        inline_graph, tmp_path, monkeypatch, stateful, supplied):
    import asyncio
    from api_agent.workflows.definition import load_native_catalog
    from api_agent.workflows.execution import Workflow, NodeFunction, WorkflowError
    from api_agent.workflows.stateful import StatefulWorkflow, load_stateful_catalog
    entry, _ = stateful_graph(tmp_path) if stateful else inline_graph
    add_timeout(entry)
    definition = admit(entry, stateful)
    monkeypatch.delenv('API_AGENT_ALLURE_REPORTING', raising=False)
    factory = WorkflowObservationFactory({}, boundary=EmissionBoundary(ContentPolicy(False)))
    recorded = []
    original_create = factory.create
    def create(*args, **kwargs):
        observation = original_create(*args, **kwargs)
        recorded.append(observation)
        return observation
    monkeypatch.setattr(factory, 'create', create)
    services = {'observation_factory': factory}
    config = {'BUDGET': 7.0}
    if stateful:
        catalog = load_stateful_catalog(definition, config=config)
        workflow = StatefulWorkflow(definition, catalog=catalog, reducers={}, config=config, services=services)
        inputs = {'seen': False}
        options = {'thread_id': 'synthetic'}
    else:
        native = load_native_catalog(definition, tmp_path / 'native.py')
        workflow = Workflow(definition, {'inspect': NodeFunction(native['inspect'])}, config=config, services=services)
        inputs, options = {}, {}
    expected = workflow_identity(definition, timeouts={'GRAPH': 7.0},
                                 component_passes={} if stateful else {'group': 1})
    if supplied:
        wrong = dict(expected, settings_sha256='0'*64)
        journal = WorkflowJournal(definition, semantic_identity=wrong)
        if stateful:
            options['journal'] = journal
        else:
            workflow.services['journal'] = journal
        with pytest.raises((ValueError, WorkflowError), match='semantic identity'):
            asyncio.run(workflow.arun(inputs, **options))
        assert journal.finalize()['events'] == []
        legacy = factory.create(definition)
        if stateful:
            options['journal'] = legacy
        else:
            workflow.services['journal'] = legacy
        asyncio.run(workflow.arun(inputs, **options))
        assert legacy.semantic_identity == expected
        return
    monkeypatch.setattr('api_agent.workflows.provenance.verified_documents', lambda *a, **k: None)
    monkeypatch.setattr('api_agent.workflows.stateful._source_manifest', lambda *a, **k: None)
    packets = []
    monkeypatch.setattr('api_agent.workflows.evidence.publish', packets.append)
    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING', '1')
    asyncio.run(workflow.arun(inputs, **options))
    packet = packets[0]
    assert recorded[0].semantic_identity == expected
    assert packet['semantic_identity'] == expected
    assert packet['semantic_identity'] != workflow_identity(definition, timeouts={'GRAPH': 3.0},
        component_passes={} if stateful else {'group': 1})
    validate_journal(packet, expected_identity=expected)


@pytest.mark.parametrize('prompt_source', ['group.md.html', 'settings.toml'])
def test_full_byte_prompt_consumption_overrides_declaration_or_budget_trimming(inline_graph, prompt_source):
    entry, component = inline_graph
    if prompt_source == 'settings.toml':
        add_timeout(entry)
    component.write_text(component.read_text().replace('| inspect | @greeting | always |',
        f'| inspect | [prompt]({prompt_source}) | always |'))
    first = declaration_identity(admit(entry))
    target = entry.parent / prompt_source
    target.write_text(target.read_text() + ('\n## Notes\n\nChanged prompt bytes.\n'
        if prompt_source == 'group.md.html' else '\n# Changed prompt bytes.\n'))
    assert declaration_identity(admit(entry)) != first


def test_order_binding_and_replacement_selection_affect_declaration(inline_graph):
    from api_agent.workflows.definition import Replacement
    entry, _ = inline_graph
    definition = admit(entry)
    expected = declaration_identity(definition)
    reversed_steps = replace(definition, layout=replace(definition.layout,
        steps=tuple(reversed(definition.layout.steps))))
    assert declaration_identity(reversed_steps) != expected
    bindings = [{'Node': 'inspect', 'Input': 'value', 'From': 'START',
                 'Field': 'value', 'Default': {'nested': [1, True]}}]
    bound = replace(definition, layout=replace(definition.layout,
        tables={**definition.layout.tables, 'Bindings': bindings}))
    assert declaration_identity(bound) != expected
    selected = load_definition(entry, base_root=entry.parent, pack_root=entry.parent,
        replacements=(Replacement('group', 'group.md.html', 'base'),))
    assert declaration_identity(selected) != expected


def test_nested_default_and_dependency_names_are_semantic(inline_graph, tmp_path):
    entry, _ = inline_graph
    definition = admit(entry)
    def with_default(value):
        return replace(definition, layout=replace(definition.layout, tables={
            **definition.layout.tables, 'Bindings': [{'Node': 'inspect', 'Default': value}]}))
    assert declaration_identity(with_default({'line': 1, '_owner': 'first'})) != declaration_identity(
        with_default({'line': 2, '_owner': 'first'}))
    assert declaration_identity(with_default({'_owner': 'first'})) != declaration_identity(
        with_default({'_owner': 'second'}))
    entry, _ = stateful_graph(tmp_path)
    definition = admit(entry, True)
    with_line = replace(definition, native_dependencies={'line': tmp_path / 'native.py'})
    assert declaration_identity(with_line) != declaration_identity(definition)


from test_workflow_retry_budget_bound import guarded_graph


def test_linked_pass_default_scope_and_actual_override_recording(guarded_graph, monkeypatch):
    import asyncio
    from api_agent.workflows import Workflow, NodeFunction
    from api_agent.workflows.definition import load_native_catalog
    entry, component = guarded_graph
    config = entry.parent / 'passes.toml'
    config.write_text('version = 1\n[fields.PASSES]\ntype = "int"\ndefault = 2\n[fields.UNRELATED]\ntype = "int"\ndefault = 1\n')
    component.write_text(component.read_text().replace('| group | 99 |', '| group | [PASSES](passes.toml) |'))
    definition = admit(entry)
    expected = workflow_identity(definition, timeouts={}, component_passes={'group': 3})
    config.write_text(config.read_text().replace('default = 1', 'default = 9'))
    assert workflow_identity(admit(entry), timeouts={}, component_passes={'group': 3}) == expected
    config.write_text(config.read_text().replace('default = 2', 'default = 4'))
    assert declaration_identity(admit(entry)) != expected['declaration_sha256']
    # Execute the original admitted snapshot, including its original config bytes.
    monkeypatch.delenv('API_AGENT_ALLURE_REPORTING', raising=False)
    native = load_native_catalog(definition, entry.parent / 'native.py')
    workflow = Workflow(definition, {'accept': NodeFunction(native['accept'])}, config={'PASSES': 3})
    packets = []
    monkeypatch.setattr('api_agent.workflows.provenance.verified_documents', lambda *a, **k: None)
    monkeypatch.setattr('api_agent.workflows.evidence.publish', packets.append)
    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING', '1')
    asyncio.run(workflow.arun({}))
    assert packets[0]['semantic_identity'] == expected
    assert packets[0]['projection']['component_passes'][0]['max_passes'] == 3


def test_admitted_native_module_alias_named_line_is_preserved(tmp_path):
    entry, _ = stateful_graph(tmp_path)
    (tmp_path / 'src').mkdir()
    (tmp_path / 'src/line.py').write_text('value = 1\n')
    entry.write_text(entry.read_text() + '\n## Native Dependencies\n\n| Module | Source |\n| --- | --- |\n'
        '| line | [python](src/line.py) |\n| src.line | [python](src/line.py) |\n')
    first = admit(entry, True)
    entry.write_text(entry.read_text().replace('| line | [python](src/line.py) |\n', ''))
    second = admit(entry, True)
    assert [(r.kind, r.logical_path, r.sha256) for r in first.resources if r.kind != 'declaration'] == [
        (r.kind, r.logical_path, r.sha256) for r in second.resources if r.kind != 'declaration']
    assert declaration_identity(first) != declaration_identity(second)


@pytest.mark.parametrize('late', ['event', 'started', 'finalized', 'gap'])
def test_identity_cannot_be_added_after_retention(inline_graph, late):
    entry, _ = inline_graph
    definition = admit(entry)
    journal = WorkflowJournal(definition)
    if late == 'event':
        journal.observe(dict(event='scope_start', scope_id='scope', kind='graph', owner='GRAPH', started_ms=1))
    elif late == 'started':
        with pytest.raises(ValueError, match='Invalid workflow observation'):
            journal.observe(None)
        assert journal._events == []
    elif late == 'finalized':
        journal.finalize()
    else:
        journal._gap = 'retention_limit'
    with pytest.raises(ValueError, match='fresh journal'):
        journal.admit_semantic_identity(budgets(definition))
    assert journal.semantic_identity is None


def test_fresh_observation_can_bind_once_and_same_identity_does_not_relabel(inline_graph):
    entry, _ = inline_graph
    definition = admit(entry)
    factory = WorkflowObservationFactory({}, boundary=EmissionBoundary(ContentPolicy(False)))
    observed = factory.create(definition)
    expected = budgets(definition)
    observed.admit_semantic_identity(expected)
    observed.observe(dict(event='scope_start', scope_id='scope', kind='graph', owner='GRAPH', started_ms=1))
    observed.admit_semantic_identity(expected)
    with pytest.raises(ValueError, match='conflicts'):
        observed.admit_semantic_identity(dict(expected, settings_sha256='0'*64))
    assert observed.finalize()['semantic_identity'] == expected


def test_binding_defaults_use_admitted_json_values_not_cell_spacing(tmp_path):
    from test_workflow_definition import make_graph
    entry = make_graph(tmp_path)
    (tmp_path / 'schemas/input.json').write_text(
        '{"type":"object","properties":{"value":{"type":"object"}}}')
    entry.write_text(entry.read_text() + '| inspect | value | START | value | {"line":1,"_owner":"one"} |\n')
    first = admit(entry)
    entry.write_text(entry.read_text().replace('{"line":1,"_owner":"one"}', '{"line": 1, "_owner": "one"}'))
    assert declaration_identity(admit(entry)) == declaration_identity(first)
    entry.write_text(entry.read_text().replace('"line": 1', '"line": 2'))
    assert declaration_identity(admit(entry)) != declaration_identity(first)


def test_normal_local_application_records_identity_through_real_factory(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.control import OperationContext
    from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent
    from api_agent.workflows.local_application import LocalChatApplication
    monkeypatch.delenv('API_AGENT_ALLURE_REPORTING', raising=False)
    root = Path(__file__).resolve().parents[2]
    definition = load_definition(root / 'flow.md.html', base_root=root, pack_root=root)
    retained = []
    class RetainedFactory(WorkflowObservationFactory):
        def create(self, *args, **kwargs):
            observed = super().create(*args, **kwargs)
            assert observed.semantic_identity is None
            retained.append(observed.journal)
            return observed
    factory = RetainedFactory({}, boundary=EmissionBoundary(ContentPolicy(False)))
    application = LocalChatApplication(definition, observations=factory)
    answer = LLMResult(Message('assistant', (TextContent('synthetic answer'),)))
    provider = SimpleNamespace(complete=AsyncMock(return_value=answer))
    request = LLMRequest('synthetic', CompletionRequest((Message('user', (TextContent('question'),)),)))
    assert asyncio.run(application.complete(request, llm=provider, context=OperationContext('request'))) is answer
    from api_agent.workflows.execution import resolve_timeouts, resolve_component_passes
    expected = workflow_identity(definition,
        timeouts=resolve_timeouts(definition.layout, application.config, definition)[0],
        component_passes=resolve_component_passes(definition.layout, application.config)[0])
    packet = retained[0].finalize()
    assert packet['events']
    assert packet['semantic_identity'] == expected
    validate_journal(packet, expected_identity=expected)


@pytest.mark.parametrize('stateful', [False, True])
def test_legacy_journal_protocol_without_optional_identity_method(inline_graph, tmp_path, stateful):
    import asyncio
    from api_agent.workflows.definition import load_native_catalog
    from api_agent.workflows.execution import Workflow, NodeFunction
    from api_agent.workflows.stateful import StatefulWorkflow, load_stateful_catalog
    entry, _ = stateful_graph(tmp_path) if stateful else inline_graph
    definition = admit(entry, stateful)
    journal = WorkflowJournal(definition)
    class LegacyJournal:
        def __getattr__(self, name):
            if name in ('semantic_identity', 'admit_semantic_identity'):
                raise AttributeError(name)
            return getattr(journal, name)
    legacy = LegacyJournal()
    if stateful:
        workflow = StatefulWorkflow(definition, catalog=load_stateful_catalog(definition),
                                    reducers={}, journal=legacy)
        asyncio.run(workflow.arun({'seen': False}, thread_id='legacy'))
    else:
        native = load_native_catalog(definition, tmp_path / 'native.py')
        workflow = Workflow(definition, {'inspect': NodeFunction(native['inspect'])}, services={'journal': legacy})
        asyncio.run(workflow.arun({}))
    assert journal.semantic_identity is None
    validate_journal(journal.finalize())


def test_admitted_integer_timeout_overflow_has_normal_validation_error(inline_graph):
    from api_agent.workflows.execution import resolve_timeouts
    entry, _ = inline_graph
    add_timeout(entry)
    config = entry.parent / 'settings.toml'
    config.write_text(config.read_text().replace('type = "float"', 'type = "int"').replace('default = 3.0', 'default = 3'))
    definition = admit(entry)
    huge = 10 ** 1000
    timeouts, keys = resolve_timeouts(definition.layout, {'BUDGET': huge}, definition)
    assert timeouts == {'GRAPH': huge} and keys == {'GRAPH': 'BUDGET'}
    with pytest.raises(ValueError, match='Workflow timeout must be finite positive seconds or None'):
        workflow_identity(definition, timeouts=timeouts, component_passes={'group': 1})
