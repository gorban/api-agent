"""Implementation-declared operations are validated before any factory is admitted."""
from pathlib import Path

import pytest

from api_agent.tooling.declarations import implementation_declaration
from api_agent.tooling.models import ToolSelectionError
from api_agent.tooling.operations import FIXED_OPERATIONS, OPERATIONS_INTERFACE, declared_operations
from api_agent.workflows.definition import _tool_operations
from api_agent.workflows.layout import LayoutError


def declaration(**changes):
    data = {'version': 1, 'interface': OPERATIONS_INTERFACE, 'factory': 'factory.py:create_tool',
            'operations': {'introspect': {'idempotent': True}, 'mutate': {}}}
    data.update(changes)
    return data


def test_valid_declaration_exposes_which_operations_are_idempotent():
    data = implementation_declaration(declaration())

    assert declared_operations(data) == {'introspect': True, 'mutate': False}


def test_an_operation_is_non_idempotent_unless_it_declares_otherwise():
    operations = {'a': {}, 'b': {'idempotent': False}, 'c': {'idempotent': True}}

    data = implementation_declaration(declaration(operations=operations))

    assert declared_operations(data) == {'a': False, 'b': False, 'c': True}


def test_other_interfaces_declare_no_operations():
    assert declared_operations({'interface': 'verified-read@1'}) == {}


@pytest.mark.parametrize('operations, message', [
    (None, 'requires an operations table'),
    ([], 'requires an operations table'),
    ({}, 'at least one operation'),
    ({'Query': {'idempotent': True}}, 'invalid operation name'),
    ({'1query': {'idempotent': True}}, 'invalid operation name'),
    ({'a' * 65: {'idempotent': True}}, 'invalid operation name'),
    ({'query__x': {'idempotent': True}}, 'invalid operation name'),
    ({'class': {'idempotent': True}}, 'reserved operation name'),
    ({'import': {'idempotent': True}}, 'reserved operation name'),
    ({'describe': {'idempotent': True}}, 'reserved operation name'),
    ({'execute': {}}, 'reserved operation name'),
    ({'invoke': {'idempotent': True}}, 'reserved operation name'),
    ({'aclose': {'idempotent': True}}, 'reserved operation name'),
    ({'query': 'read'}, 'must be a table'),
    ({'query': {'idempotent': 'yes'}}, 'idempotent'),
    ({'query': {'idempotent': 1}}, 'idempotent'),
    ({'query': {'effect': 'read'}}, 'idempotent'),
    ({'query': {'idempotent': True, 'extra': True}}, 'idempotent'),
])
def test_invalid_operations_reject(operations, message):
    data = declaration()
    if operations is None:
        del data['operations']
    else:
        data['operations'] = operations

    with pytest.raises(ToolSelectionError, match=message):
        implementation_declaration(data)


def test_verified_read_implementation_cannot_declare_operations():
    data = declaration(interface='verified-read@1')

    with pytest.raises(ToolSelectionError, match='Only operations@1'):
        implementation_declaration(data)


@pytest.mark.parametrize('value', ['introspect', 'query, mutate', 'a0_b1', *FIXED_OPERATIONS])
def test_node_grammar_accepts_declared_style_names(value):
    operations = _tool_operations(Path('flow.md'), {'Operations': value, 'line': 1}, 'node')

    assert operations == tuple(part.strip() for part in value.split(','))


@pytest.mark.parametrize('value', ['Query', 'query query', 'class', 'invoke', 'query,,mutate', 'query__'])
def test_node_grammar_rejects_malformed_or_reserved_names(value):
    with pytest.raises(LayoutError, match='operations'):
        _tool_operations(Path('flow.md'), {'Operations': value, 'line': 1}, 'node')
