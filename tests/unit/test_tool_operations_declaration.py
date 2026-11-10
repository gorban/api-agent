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
            'operations': {'introspect': {'effect': 'read'}, 'mutate': {'effect': 'write'}}}
    data.update(changes)
    return data


def test_valid_declaration_exposes_ordered_effects():
    data = implementation_declaration(declaration())

    assert declared_operations(data) == {'introspect': 'read', 'mutate': 'write'}


def test_other_interfaces_declare_no_operations():
    assert declared_operations({'interface': 'verified-read@1'}) == {}


@pytest.mark.parametrize('operations, message', [
    (None, 'requires an operations table'),
    ([], 'requires an operations table'),
    ({}, 'at least one operation'),
    ({'Query': {'effect': 'read'}}, 'invalid operation name'),
    ({'1query': {'effect': 'read'}}, 'invalid operation name'),
    ({'a' * 65: {'effect': 'read'}}, 'invalid operation name'),
    ({'query__x': {'effect': 'read'}}, 'invalid operation name'),
    ({'class': {'effect': 'read'}}, 'reserved operation name'),
    ({'import': {'effect': 'read'}}, 'reserved operation name'),
    ({'describe': {'effect': 'read'}}, 'reserved operation name'),
    ({'execute': {'effect': 'write'}}, 'reserved operation name'),
    ({'invoke': {'effect': 'read'}}, 'reserved operation name'),
    ({'aclose': {'effect': 'read'}}, 'reserved operation name'),
    ({'query': 'read'}, 'must be a table'),
    ({'query': {}}, 'effect'),
    ({'query': {'effect': 'delete'}}, 'effect'),
    ({'query': {'effect': 'read', 'extra': True}}, 'effect'),
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
