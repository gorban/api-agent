"""Managed workflow capture defaults do not elevate other credential owners."""
from types import SimpleNamespace

import pytest

from api_agent.providers.contracts import ContractError
from api_agent.testing.authority_scope import current_scope
from api_agent.telemetry.privacy import EmissionBoundary
from api_agent.workflows.capture_authority import resolve_capture_services
from test_workflow_capture_context import snapshot


@pytest.fixture
def managed_owned_worker(monkeypatch):
    from api_agent.testing import provider_setup
    monkeypatch.setenv('API_AGENT_INTEGRATION_CONTEXT', 'synthetic-context')
    data = snapshot()
    data['config']['TUI_MODE'] = 'e2e'
    monkeypatch.setattr(provider_setup, '_test_snapshot', lambda _: data)
    request = SimpleNamespace(node=SimpleNamespace(get_closest_marker=lambda _: None), fixturenames=())
    fixture = provider_setup._test_credential_authority_scope.__wrapped__(request)
    next(fixture)
    try:
        yield current_scope()
    finally:
        fixture.close()


@pytest.mark.asyncio
async def test_owned_capture_exits_inside_async_task_before_pytest_teardown(managed_owned_worker):
    import asyncio
    from api_agent.testing.workflow_capture import provider_capture, register_owned_ui_access
    authority = EmissionBoundary()
    register_owned_ui_access(authority, 'synthetic-owned-ui-token')
    with provider_capture(SimpleNamespace(emissions=authority)):
        await asyncio.sleep(0)
        lease = managed_owned_worker.freeze(credential_only=True)
        lease.release()
    assert authority in managed_owned_worker.authorities()


def test_owned_ui_access_joins_real_managed_freeze_and_redacts_token(monkeypatch):
    from api_agent.testing import provider_setup
    from api_agent.testing.workflow_capture import provider_capture, register_owned_ui_access
    monkeypatch.setenv('API_AGENT_INTEGRATION_CONTEXT', 'synthetic-context')
    data = snapshot()
    data['config']['TUI_MODE'] = 'e2e'
    monkeypatch.setattr(provider_setup, '_test_snapshot', lambda _: data)
    request = SimpleNamespace(node=SimpleNamespace(get_closest_marker=lambda _: None), fixturenames=())
    fixture = provider_setup._test_credential_authority_scope.__wrapped__(request)
    next(fixture)
    try:
        scope = current_scope()
        from api_agent.telemetry.privacy import ContentPolicy
        authority = EmissionBoundary(ContentPolicy(True))
        with provider_capture(SimpleNamespace(emissions=authority)):
            with pytest.raises(ContractError, match='unavailable'):
                scope.freeze(credential_only=True)
            register_owned_ui_access(authority, 'synthetic-owned-ui-token')
            lease = scope.freeze(credential_only=True)
            try:
                assert 'synthetic-owned-ui-token' in lease.secrets
                assert authority.content_export({'message': 'synthetic-owned-ui-token'}) == {
                    'message': '[REDACTED]'}
            finally:
                lease.release()
    finally:
        fixture.close()


@pytest.mark.parametrize('manifest', [None, ('coordinator',), ('workflow',), ('ui',), ('tools', 'ui')])
@pytest.mark.parametrize('log', [False, True])
def test_managed_worker_always_binds_workflow_capture(monkeypatch, manifest, log):
    from api_agent.testing import provider_setup
    marker = None if manifest is None else SimpleNamespace(args=manifest, kwargs={})
    request = SimpleNamespace(node=SimpleNamespace(get_closest_marker=lambda _: marker))
    monkeypatch.setenv('API_AGENT_INTEGRATION_CONTEXT', 'synthetic-context')
    monkeypatch.setattr(provider_setup, '_test_snapshot', lambda _: snapshot(log=log))
    fixture = provider_setup._test_credential_authority_scope.__wrapped__(request)

    next(fixture)
    try:
        services = resolve_capture_services({})
        assert services['content_policy'].allow_content is log
        assert services['emissions'].content_export({'text': 'local-secret'}) == {'text': '[REDACTED]'}
        for owner in (manifest or ()):
            if owner in ('ui', 'tools'):
                current_scope().join(owner, EmissionBoundary())
        assert services['emissions'] in current_scope().authorities()
    finally:
        fixture.close()
    assert not resolve_capture_services({})
    assert current_scope() is None


def test_managed_default_does_not_admit_undeclared_tool_credentials(monkeypatch):
    from api_agent.testing import provider_setup
    request = SimpleNamespace(node=SimpleNamespace(get_closest_marker=lambda _: None))
    monkeypatch.setenv('API_AGENT_INTEGRATION_CONTEXT', 'synthetic-context')
    monkeypatch.setattr(provider_setup, '_test_snapshot', lambda _: snapshot())
    fixture = provider_setup._test_credential_authority_scope.__wrapped__(request)
    next(fixture)
    try:
        with pytest.raises(ContractError, match='Undeclared'):
            current_scope().join('tools', EmissionBoundary())
    finally:
        fixture.close()


@pytest.mark.parametrize('reason', ['captured', 'partial', 'truncated', 'withheld', 'unavailable',
    'privacy_withheld', 'unclassified', 'unsupported', 'payload_limit', None, {'secret': 'detail'}])
def test_node_capture_states_are_bounded_and_missing_output_is_explained(reason):
    from test_workflow_steps import trace
    from api_agent.testing.workflow_steps import scope_steps
    source = trace()
    start, stop = dict(source[1], kind='node'), dict(source[-2], kind='node')
    events = [source[0], start, dict(event='capture', visit_id=start['scope_id'],
        direction='input', reason=reason, values={}), stop, source[-1]]

    node = scope_steps(events)[0]['steps'][0]

    parameters = {item['name']: item['value'] for item in node['parameters']}
    expected = reason if type(reason) is str else 'unknown'
    assert parameters['input_capture'] == expected
    assert parameters['input_capture_detail']
    assert parameters['output_capture'] == 'not_observed'
    assert 'No capture event' in parameters['output_capture_detail']
    assert 'secret' not in str(parameters)


def test_call_capture_cannot_replace_node_io_and_invalid_phase_is_ignored():
    from uuid import uuid4
    from test_workflow_steps import trace
    from api_agent.testing.workflow_steps import scope_steps
    source = trace()
    start, stop = dict(source[1], kind='node'), dict(source[-2], kind='node')
    events = [source[0], start,
        dict(event='capture', visit_id=start['scope_id'], direction='input', reason='captured', values={}),
        dict(event='capture', visit_id=start['scope_id'], direction='input', reason='withheld', cause_id=str(uuid4())),
        dict(event='capture', visit_id=start['scope_id'], direction='unexpected', reason='captured'),
        dict(event='capture', visit_id='absent', direction='output', reason='captured'), stop, source[-1]]

    node = scope_steps(events)[0]['steps'][0]

    parameters = {item['name']: item['value'] for item in node['parameters']}
    assert parameters['input_capture'] == 'captured'
    assert parameters['output_capture'] == 'not_observed'
    assert not any(name.startswith('unexpected') for name in parameters)


@pytest.mark.parametrize('manifest', [(), ('ui', 'ui'), ('coordinator', 'tools'), ('unknown',), 'workflow', (1,)])
def test_managed_defaults_do_not_repair_invalid_manifests(manifest):
    from api_agent.testing.workflow_capture import managed_workflow_manifest
    with pytest.raises(ContractError, match='manifest'):
        managed_workflow_manifest(manifest)


@pytest.mark.asyncio
async def test_missing_declared_owner_remains_unavailable_with_default_workflow(monkeypatch):
    from api_agent.testing import provider_setup
    marker = SimpleNamespace(args=('tools',), kwargs={})
    request = SimpleNamespace(node=SimpleNamespace(get_closest_marker=lambda _: marker))
    monkeypatch.setenv('API_AGENT_INTEGRATION_CONTEXT', 'synthetic-context')
    monkeypatch.setattr(provider_setup, '_test_snapshot', lambda _: snapshot())
    fixture = provider_setup._test_credential_authority_scope.__wrapped__(request)
    next(fixture)
    try:
        with pytest.raises(ContractError, match='unavailable'):
            async with resolve_capture_services({})['capture_authority']():
                pytest.fail('Missing declared credential producer was admitted')
    finally:
        fixture.close()


def test_duplicate_direct_node_phases_are_ambiguous_not_last_writer_wins():
    from test_workflow_steps import trace
    from api_agent.testing.workflow_steps import scope_steps
    source = trace()
    start, stop = dict(source[1], kind='node'), dict(source[-2], kind='node')
    captures = [dict(event='capture', visit_id=start['scope_id'], direction='input', reason=reason)
                for reason in ('withheld', 'captured', 'captured')]

    node = scope_steps([source[0], start, *captures, stop, source[-1]])[0]['steps'][0]

    parameters = {item['name']: item['value'] for item in node['parameters']}
    assert parameters['input_capture'] == 'ambiguous'
    assert 'Multiple direct' in parameters['input_capture_detail']
    assert len([p for p in node['parameters'] if p['name'] == 'input_capture']) == 1


@pytest.mark.parametrize('log', [False, True])
@pytest.mark.parametrize('plain', [False, True])
@pytest.mark.parametrize('direction', ['input', 'output'])
def test_missing_node_authority_never_receives_original_values(log, plain, direction):
    import asyncio
    from api_agent.telemetry.privacy import ContentPolicy
    from api_agent.workflows.capture_authority import capture_node
    from test_tool_observations import observed_journal, VISIT
    journal, _ = observed_journal(log=log)
    schema = {'type': 'object', 'properties': {'text': {'type': 'string'}}}

    row = asyncio.run(capture_node(journal, {}, visit_id=VISIT, direction=direction,
        values={'text': 'unregistered-sensitive-value'}, schema=schema,
        **({'plain_fields': ['text']} if plain else {})))

    assert row['reason'] == 'unavailable'
    assert not row.get('values')
    assert 'unregistered-sensitive-value' not in str(journal.finalize())


@pytest.mark.parametrize('log', [False, True])
def test_declared_engine_missing_authority_is_unavailable_without_losing_answer(log):
    import asyncio
    from api_agent.providers.llm import LLMResult, Message, TextContent
    from api_agent.telemetry.privacy import ContentPolicy
    from api_agent.workflows.application import build
    from api_agent.workflows.journal import WorkflowJournal

    async def complete(request):
        return LLMResult(Message('assistant', (TextContent('response-sensitive-value'),)))

    graph = build(llm=complete, selection='synthetic', config={'LOG_CONTENT': log})
    journal = WorkflowJournal(graph.definition, log_content=log, content_policy=ContentPolicy(log))
    graph.services = dict(graph.services, journal=journal, content_policy=ContentPolicy(log))

    result = asyncio.run(graph.arun({'messages': [{'role': 'user', 'content': 'input-sensitive-value'}]}))

    assert result['llm_call']['response_text'] == 'response-sensitive-value'
    packet = journal.finalize()
    captures = [event for event in packet['events'] if event['event'] == 'capture']
    assert [event['direction'] for event in captures] == ['input', 'output']
    assert all(event['reason'] == 'unavailable' and not event.get('values') for event in captures)
    assert 'sensitive-value' not in str(packet)


from test_workflow_stateful import fixture_graph


@pytest.mark.parametrize('log', [False, True])
def test_stateful_engine_missing_authority_is_unavailable_without_losing_state(fixture_graph, log):
    import asyncio
    from langgraph.graph.message import add_messages
    from api_agent.telemetry.privacy import ContentPolicy
    from api_agent.workflows.journal import WorkflowJournal
    from api_agent.workflows.execution import (Workflow)
    from api_agent.workflows.native import (load_native_bindings)
    from api_agent.workflows.definition import (load_definition)
    root, entry = fixture_graph
    definition = load_definition(entry, base_root=root, pack_root=root)
    journal = WorkflowJournal(definition, log_content=log, content_policy=ContentPolicy(log))
    graph = Workflow(definition, catalog=load_native_bindings(definition),
        reducers={'messages': add_messages}, journal=journal, config={'LOG_CONTENT': log})

    result = asyncio.run(graph.arun({'messages': [], 'pending': False, 'visits': 0}, thread_id='missing-authority'))

    assert result['visits'] == 1
    captures = [event for event in journal.finalize()['events'] if event['event'] == 'capture']
    assert len(captures) == 4
    assert all(event['reason'] == 'unavailable' and not event.get('values') for event in captures)


@pytest.mark.parametrize('reason', ['captured', 'partial', 'truncated'])
@pytest.mark.parametrize('retained', [False, True])
def test_capture_status_does_not_claim_retained_payload_when_values_are_missing(reason, retained):
    from test_workflow_steps import trace
    from api_agent.testing.workflow_steps import scope_steps
    source = trace()
    start, stop = dict(source[1], kind='node'), dict(source[-2], kind='node')
    event = dict(event='capture', visit_id=start['scope_id'], direction='input', reason=reason)
    if retained:
        event['values'] = None

    node = scope_steps([source[0], start, event, stop, source[-1]])[0]['steps'][0]

    parameters = {item['name']: item['value'] for item in node['parameters']}
    assert parameters['input_capture'] == (reason if retained else 'unavailable')


@pytest.mark.parametrize('field', ['cause_id', 'transport_attempt_id', 'capture_source'])
def test_transport_capture_metadata_does_not_supply_node_payload_status(field):
    from uuid import uuid4
    from test_workflow_steps import trace
    from api_agent.testing.workflow_steps import scope_steps
    source = trace()
    start, stop = dict(source[1], kind='node'), dict(source[-2], kind='node')
    event = dict(event='capture', visit_id=start['scope_id'], direction='input',
                 reason='captured', values={})
    event[field] = 'provider' if field == 'capture_source' else str(uuid4())

    node = scope_steps([source[0], start, event, stop, source[-1]])[0]['steps'][0]

    parameters = {item['name']: item['value'] for item in node['parameters']}
    assert parameters['input_capture'] == 'not_observed'


@pytest.mark.parametrize('fixtures,mode,owners', [
    (('workflow_tools',), 'integration', ('tools',)),
    (('ui_provider_session',), 'integration', ('ui',)),
    (('workflow_tools', 'ui_provider_session'), 'integration', ('tools', 'ui')),
    ((), 'e2e', ('ui',)),
    (('workflow_tools',), 'e2e', ('tools', 'ui')),
])
def test_unmarked_managed_fixture_enrolls_known_owners_but_requires_their_registration(monkeypatch, fixtures, mode, owners):
    from api_agent.testing import provider_setup
    request = SimpleNamespace(node=SimpleNamespace(get_closest_marker=lambda _: None), fixturenames=fixtures)
    data = snapshot()
    data['config']['TUI_MODE'] = mode
    monkeypatch.setenv('API_AGENT_INTEGRATION_CONTEXT', 'synthetic-context')
    monkeypatch.setattr(provider_setup, '_test_snapshot', lambda _: data)
    fixture = provider_setup._test_credential_authority_scope.__wrapped__(request)
    next(fixture)
    try:
        with pytest.raises(ContractError, match='unavailable'):
            current_scope().authorities()
        for owner in owners:
            current_scope().join(owner, EmissionBoundary())
        assert len(current_scope().authorities()) == 1 + len(owners)
        undeclared = 'ui' if 'ui' not in owners else 'tools' if 'tools' not in owners else None
        if undeclared is not None:
            with pytest.raises(ContractError, match='Undeclared'):
                current_scope().join(undeclared, EmissionBoundary())
    finally:
        fixture.close()


def test_explicit_managed_manifest_remains_closed_even_when_other_fixtures_are_present():
    from api_agent.testing.workflow_capture import managed_workflow_manifest
    assert managed_workflow_manifest(('coordinator',), fixture_names=('workflow_tools',), mode='e2e') == ('workflow',)
    assert managed_workflow_manifest(('ui',), fixture_names=('workflow_tools',)) == ('ui', 'workflow')


@pytest.mark.parametrize('boundary,token', [(None, 'secret'),
    (EmissionBoundary(), None), (EmissionBoundary(), '')])
def test_owned_ui_rejects_invalid_credential_authority(boundary, token):
    from api_agent.testing.workflow_capture import register_owned_ui_access
    with pytest.raises(ContractError, match='Invalid owned UI'):
        register_owned_ui_access(boundary, token)


def test_unmanaged_owned_ui_registers_secret_without_a_managed_scope():
    from api_agent.testing.workflow_capture import register_owned_ui_access
    from api_agent.telemetry.privacy import ContentPolicy
    assert current_scope() is None
    boundary = EmissionBoundary(ContentPolicy(True))
    register_owned_ui_access(boundary, 'owned-ui-secret')
    assert 'owned-ui-secret' not in str(boundary.content_export({'token': 'owned-ui-secret'}))
