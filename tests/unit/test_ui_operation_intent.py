"""Selected UI intent preserves declarative applicability and ordinary precedence."""
import pytest

from api_agent.config.declarations import ConfigSchema, FieldDeclaration, schema_from_dict
from api_agent.config.resolve import ConfigError, _controllers, resolve_config


def schema():
    return schema_from_dict({'version':1, 'fields':{
        'TUI_MODE':{'type':'str', 'default':'integration'},
        'COLLECT_ONLY':{'type':'bool', 'default':False},
        'UI_LISTENER_URL':{'type':'str', 'default':'http://localhost:9443',
            'applies_when':{'any':[{'TUI_MODE':'e2e'}, {'operation':'ui_session'}], 'COLLECT_ONLY':False}}}})


def test_explicit_intent_activates_only_requested_fields():
    ordinary = resolve_config(schema(), environ={'operation':'ui_session'})
    selected = resolve_config(schema(), environ={}, operation_intents=('ui_session',))
    assert 'UI_LISTENER_URL' not in ordinary.values
    assert selected.values['UI_LISTENER_URL'] == 'http://localhost:9443'
    assert selected.values['TUI_MODE'] == 'integration'
    assert tuple(_controllers({'any':[{'operation':'ui_session'}, {'TUI_MODE':'e2e'}]})) == ('TUI_MODE',)


@pytest.mark.parametrize('external,source', [({'environ':{'UI_LISTENER_URL':'http://localhost:9001'}},'environment'),
    ({'dotenv':{'UI_LISTENER_URL':'http://localhost:9002'}},'dotenv'),
    ({'argv':['--ui-listener-url=http://localhost:9003']},'cli')])
def test_intent_retains_configuration_sources(external, source):
    result = resolve_config(schema(), operation_intents=('ui_session',), **external)
    assert result.fields['UI_LISTENER_URL'].source == source
    assert result.values['UI_LISTENER_URL'].startswith('http://localhost:900')


def test_intent_does_not_bypass_other_conditions_or_pack_scope():
    declarations = schema()
    declarations = ConfigSchema({**declarations.fields,
        'PRIVATE_UI':FieldDeclaration('PRIVATE_UI','str',default='hidden',pack_only=True,
            applies_when={'operation':'ui_session'})})
    result = resolve_config(declarations, environ={'COLLECT_ONLY':'True'}, operation_intents=('ui_session',))
    assert 'UI_LISTENER_URL' not in result.values
    assert 'PRIVATE_UI' not in result.values


@pytest.mark.parametrize('where', ['required_when','conditional_defaults'])
def test_intent_cannot_change_defaults_or_requirements(where):
    spec = {'type':'str'}
    spec[where] = ([{'when':{'operation':'ui_session'},'value':'bad'}]
                  if where == 'conditional_defaults' else {'operation':'ui_session'})
    with pytest.raises(ValueError,match='operation intent'):
        schema_from_dict({'version':1,'fields':{'FIELD':spec}})


@pytest.mark.parametrize('value', ['llm', True, ['ui_session']])
def test_bad_declared_intent_rejected(value):
    with pytest.raises(ValueError,match='operation intent'):
        schema_from_dict({'version':1,'fields':{'FIELD':{'type':'str','applies_when':{'operation':value}}}})


def test_unknown_resolver_intent_rejected():
    with pytest.raises(ConfigError,match='operation intent'):
        resolve_config(schema(), operation_intents=('unknown',))
