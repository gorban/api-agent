"""Managed collector recovery explains changes without exposing their values."""
from types import SimpleNamespace

import pytest

from api_agent.runtime.compose import plan_compose
from api_agent.runtime.ownership import StackScope
from api_agent.runtime.prerequisites import PrerequisiteManager, PrerequisiteError, _interpolate
from api_agent.runtime.prerequisite_diagnostics import initialization_changes, incompatible_stack_message
from test_prerequisites import Clock, Engine, trace_fixture_config


def manager(tmp_path, engine, config):
    scope = StackScope.from_roots(tmp_path / 'base', tmp_path / 'pack')
    clock = Clock()
    result = PrerequisiteManager(plan_compose(trace_fixture_config(config), scope), scope, tmp_path / 'ownership',
        command_runner=engine.run, readiness_probe=lambda *args: True,
        now=clock.now, sleep=clock.sleep, identity=lambda pid: 'boot:start')
    engine.owner = result
    return result


@pytest.mark.parametrize('running', [False, True])
@pytest.mark.parametrize('changes', [
    {'LANGFUSE_ADMIN_EMAIL': 'new-operator@example.invalid'},
    {'LANGFUSE_ADMIN_PASSWORD': 'new-synthetic-password-with=delimiter'},
    {'LANGFUSE_ADMIN_EMAIL': 'new-operator@example.invalid',
     'LANGFUSE_ADMIN_PASSWORD': 'new-synthetic-password-with=delimiter'},
])
def test_changed_dashboard_settings_name_the_cause_and_preserve_the_stack(tmp_path, running, changes):
    config = {'TRACE_MODE': 'LangFuse', 'LANGFUSE_ADMIN_EMAIL': 'old-operator@example.invalid',
              'LANGFUSE_ADMIN_PASSWORD': 'old-synthetic-password'}
    engine = Engine()
    first = manager(tmp_path, engine, config)
    first.acquire()
    web = next(record for record in engine.records.values()
               if record['Config']['Labels']['com.docker.compose.service'] == 'langfuse-web')
    actual = _interpolate(first.specs['langfuse-web']['environment'], first.environment)
    web['Config']['Env'] = [key + '=' + value for key, value in actual.items()]
    first.release(retain=running)
    original_ids = set(engine.records)
    second = manager(tmp_path, engine, dict(config, **changes))
    before = len(engine.commands)

    with pytest.raises(PrerequisiteError, match='incompatible') as caught:
        second.acquire()

    message = str(caught.value)
    assert '\n\n\n' + '!' * 72 + '\n\nACTION REQUIRED:' in message
    assert message.endswith('\n' + '!' * 72 + '\n\n')
    for setting in changes:
        assert '\n  ' + setting in message
    assert 'could not be established' not in message
    for secret in (*config.values(), *changes.values()):
        if secret != 'LangFuse':
            assert secret not in message
    assert 'docker rm ' + web['Id'] in message
    assert 'Keep all volumes.' in message
    assert 'does not rotate an existing' in message
    assert set(engine.records) == original_ids
    assert all(record['State']['Running'] is running for record in engine.records.values())
    assert not any('up' in command or command[1] in {'stop', 'rm', 'volume', 'down'}
                   for command, _ in engine.commands[before:])


@pytest.mark.parametrize('actual', [None, {}, [], ['OTHER=value'], ['malformed'], [1],
    ['LANGFUSE_INIT_USER_PASSWORD=old', 'LANGFUSE_INIT_USER_PASSWORD=other']])
def test_unverifiable_metadata_does_not_claim_a_changed_password(actual):
    assert initialization_changes('langfuse-web', actual,
                                  {'LANGFUSE_INIT_USER_PASSWORD': 'new'}) == ()


def test_matching_metadata_and_unrelated_service_do_not_claim_changes():
    actual = ['LANGFUSE_INIT_USER_PASSWORD=identical']
    expected = {'LANGFUSE_INIT_USER_PASSWORD': 'identical'}
    assert initialization_changes('langfuse-web', actual, expected) == ()
    assert initialization_changes('jaeger', actual, {'LANGFUSE_INIT_USER_PASSWORD': 'new'}) == ()
    assert initialization_changes('langfuse-web', actual, {}) == ()


def test_unknown_change_has_qualified_advice_and_quotes_the_engine_path():
    item = SimpleNamespace(service='langfuse-web', profile='langfuse',
                           initialization_changes=(), id='a' * 64)
    text = incompatible_stack_message(item, configuration_changed=True,
                                     executable='/test tools/docker', project='sa-test')
    assert 'exact setting change could not be established' in text
    assert 'Other settings or images may differ.' in text
    assert "'/test tools/docker' rm " + item.id in text


@pytest.mark.parametrize('service,profile,changed', [
    ('jaeger', 'otel', True), ('langfuse-web', 'other', True), ('langfuse-web', 'langfuse', False)])
def test_unrelated_incompatibility_does_not_get_a_credential_recovery_notice(service, profile, changed):
    item = SimpleNamespace(service=service, profile=profile)
    assert incompatible_stack_message(item, configuration_changed=changed, executable='docker',
                                      project='sa-test') == (
        'Existing prerequisite stack is incompatible; review it before replacement.')
