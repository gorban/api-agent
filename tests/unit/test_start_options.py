"""Startup option checks using synthetic values and mocked operation boundaries."""
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from api_agent.config import load_shared_schema, resolve_config
from api_agent.runtime.bootstrap import generated_help, main
from api_agent.runtime.starting import (START_MODES, start_configuration_errors,
                                        start_requested, e2e_accepted, automatically_started)

ROOT = Path(__file__).resolve().parents[2]


def resolved(argv=(), **kwargs):
    return resolve_config(load_shared_schema(ROOT), argv=list(argv), environ=kwargs.pop('environ', {}),
                          interactive=True, anchors={'owner_root': ROOT, 'pack_root': ROOT,
                                                     'base_root': ROOT}, **kwargs)


@pytest.mark.parametrize('argv,expected', [(['--start'], 'True'), (['--start=True'], 'True'),
    (['--start', 'True'], 'True'), (['--start=False'], 'False'), (['--start', 'False'], 'False')])
def test_boolean_cli_forms(argv, expected):
    result = resolved(['--tui-mode=unit', *argv])
    assert not result.errors
    assert result.values['START'] == expected
    assert result.fields['START'].source == 'cli'
    assert result.fields['START'].explicit


@pytest.mark.parametrize('dotenv,environ,argv,expected,source', [
    ({}, {}, [], 'False', 'shared_default'),
    ({'START':'True'}, {}, [], 'True', 'dotenv'),
    ({'START':'True'}, {'START':'False'}, [], 'False', 'environment'),
    ({'START':'False'}, {'START':'False'}, ['--start'], 'True', 'cli'),
])
def test_source_precedence(dotenv, environ, argv, expected, source):
    result = resolved(['--tui-mode=unit', *argv], dotenv=dotenv, environ=environ)
    assert result.values['START'] == expected
    assert result.fields['START'].source == source
    assert result.fields['START'].explicit is (source != 'shared_default')


def test_duplicate_rejected():
    assert any('Duplicate option --start' in error for error in resolved(['--start', '--start=False']).errors)


@pytest.mark.parametrize('mode', [None, 'config-cleanup', 'cleanup-containers', 'unattended-auth'])
@pytest.mark.parametrize('collect', [False, True])
def test_start_true_requires_supported_mode_even_when_collecting(mode, collect):
    result = resolved(['--start', *(['--tui-mode='+mode] if mode else []),
                       *(['--collect-only'] if collect else [])])
    assert start_configuration_errors(result)[0].startswith('START=True or E2E requires TUI_MODE')


@pytest.mark.parametrize('mode', sorted(START_MODES))
def test_supported_mode_start_true(mode):
    assert not start_configuration_errors(resolved(['--start', '--tui-mode='+mode]))


@pytest.mark.parametrize('mode,debug,conflict', [('unit',False,True), ('run-local',False,True),
    ('allure',False,True), ('deployment-logs',False,True), ('integration',True,True),
    ('integration',False,False), ('e2e',False,False), ('config-cleanup',False,False)])
def test_explicit_false_conflicts_only_with_automatic_execution(mode, debug, conflict):
    argv = ['--tui-mode='+mode, '--no-std-in', *(['--debug'] if debug else [])]
    errors = start_configuration_errors(resolved([*argv, '--start=False']))
    assert bool(errors) is conflict
    if conflict:
        suffix = ('; collection-only is still allowed via --collect-only.'
                  if mode in {'unit', 'integration', 'e2e'} else '.')
        assert errors == ('START=False conflicts with automatic execution using NO_STD_IN=True; '
                          'omit START or set START=True' + suffix,)
    assert not start_configuration_errors(resolved(argv))
    assert not start_configuration_errors(resolved([*argv, '--start=False', '--collect-only']))
    assert not start_configuration_errors(resolved([arg for arg in [*argv, '--start=False'] if arg != '--no-std-in']))


@pytest.mark.parametrize('source', ['dotenv', 'environment'])
def test_external_explicit_false_conflict(source):
    kwargs = {source if source == 'dotenv' else 'environ': {'START':'False'}}
    assert start_configuration_errors(resolved(['--tui-mode=unit', '--no-std-in'], **kwargs))


def test_help_bypasses_start_execution_validation():
    assert not start_configuration_errors(resolved(['--help', '--start', '--tui-mode=config-cleanup']))
    assert not start_configuration_errors(resolved(['--help', '--start=False', '--no-std-in', '--tui-mode=unit']))
    text = generated_help(load_shared_schema(ROOT))
    assert '--start=E2E' in text and 'normal startup checks' in text


def test_start_defaults_hidden_and_globally_active():
    schema = load_shared_schema(ROOT)
    assert schema.fields['START'].acknowledge_default is False
    assert not schema.fields['START'].applies_when
    result = resolved()
    assert result.values['START'] == 'False' and not result.fields['START'].explicit


@pytest.mark.parametrize('argv', [['--start'], ['--start', '--tui-mode=config-cleanup'],
    ['--start=False', '--no-std-in', '--tui-mode=unit']])
def test_invalid_start_stops_before_operation_effects(argv):
    stderr = StringIO()
    with patch.dict('os.environ', {}, clear=True), patch('sys.stderr', stderr), \
            patch('api_agent.config.read_dotenv', return_value={}), \
            patch('api_agent.config.dotenv.read_dotenv', return_value={}), \
            patch('api_agent.runtime.worktrees.verify_worktrees'), \
            patch('api_agent.runtime.dependencies.ensure_environment') as prepare, \
            patch('api_agent.testing.collection.collect_tests') as collect:
        assert main(argv, base_root=ROOT) == 2
        prepare.assert_not_called()
        collect.assert_not_called()
    assert 'Configuration error: START=' in stderr.getvalue()


@pytest.mark.parametrize('start', ['True', 'False'])
def test_collect_only_preserves_nonexecuting_path(start):
    with patch.dict('os.environ', {}, clear=True), \
            patch('api_agent.config.read_dotenv', return_value={}), \
            patch('api_agent.config.dotenv.read_dotenv', return_value={}), \
            patch('api_agent.runtime.worktrees.verify_worktrees'), \
            patch('api_agent.runtime.dependencies.ensure_environment', return_value=None), \
            patch('api_agent.testing.collection.collect_tests', return_value=[]) as collect, \
            patch('api_agent.testing.runner.run_unit_tests') as run:
        assert main(['--tui-mode=unit', '--no-std-in', '--collect-only', '--start='+start], base_root=ROOT) == 0
        collect.assert_called_once()
        run.assert_not_called()


@pytest.mark.parametrize('value,requested,accepted', [('',False,False), ('False',False,False),
    ('True',True,False), ('E2E',True,True)])
def test_string_start_contract(value, requested, accepted):
    for argv in (["--start="+value], ["--start", value]):
        result = resolved(['--tui-mode=e2e', *argv])
        assert not result.errors
        assert start_requested(result.values) is requested
        assert e2e_accepted(result) is accepted
        assert automatically_started(result) is accepted


@pytest.mark.parametrize('value', ['true', 'false', 'e2e', 'invalid'])
def test_invalid_enum_rejected(value):
    assert any('START:' in error for error in resolved(['--start='+value]).errors)


def test_bare_start_preserves_selector_and_next_option():
    result = resolved(['--start', 'test_file.py', '--tui-mode=unit'])
    assert result.values['START'] == 'True'
    assert result.positionals == ('test_file.py',)
    result = resolved(['--start', '--tui-mode=unit'])
    assert not result.errors and result.values['START'] == 'True'


@pytest.mark.parametrize('mode', sorted(START_MODES))
def test_e2e_start_requests_any_supported_mode(mode):
    result = resolved(['--start=E2E', '--tui-mode='+mode])
    assert start_requested(result.values)
    assert not start_configuration_errors(result)


def test_empty_start_does_not_conflict_with_automatic_execution():
    result = resolved(['--start=', '--tui-mode=unit', '--no-std-in'])
    assert not start_requested(result.values)
    assert not start_configuration_errors(result)


@pytest.mark.parametrize('value', ['True', 'E2E'])
def test_request_guard_applies_to_both_start_values(value):
    result = resolved(['--start='+value, '--tui-mode=config-cleanup', '--collect-only'])
    assert start_configuration_errors(result)


def test_boolean_like_enum_parser_is_schema_driven():
    from api_agent.config import schema_from_dict, parse_options
    schema = schema_from_dict({'version':1, 'fields': {'ACTION': {
        'type':'str', 'enum':['', 'False', 'True', 'Accepted']}}})
    assert parse_options(schema, ['--action']).values == {'ACTION':'True'}
    assert parse_options(schema, ['--action', 'Accepted']).values == {'ACTION':'Accepted'}
    parsed = parse_options(schema, ['--action', 'test.py'])
    assert parsed.values == {'ACTION':'True'} and parsed.positionals == ('test.py',)


def test_declaration_default_cannot_accept_e2e():
    from dataclasses import replace
    from api_agent.config import schema_from_dict
    schema = schema_from_dict({'version': 1, 'fields': {
        'START': {'type': 'str', 'enum': ['', 'False', 'True', 'E2E'], 'default': 'E2E'},
        'TUI_MODE': {'type': 'str', 'default': 'e2e'}}})
    result = resolve_config(schema, environ={})
    assert start_requested(result.values) and not e2e_accepted(result)
    assert not automatically_started(result)
    result = resolve_config(schema, argv=['--start=E2E'], environ={})
    assert e2e_accepted(result) and automatically_started(result)
    assert not e2e_accepted(replace(result, fields={}))
