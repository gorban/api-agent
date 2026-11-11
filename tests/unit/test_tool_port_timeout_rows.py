"""A Timeouts row may name one declared tool port of one node as `<node>.<port>`."""
import pytest

from api_agent.workflows.configuration import declared_timeouts
from api_agent.workflows.definition import load_definition
from api_agent.workflows.identity import declaration_identity, workflow_identity
from api_agent.workflows.model import LayoutError
from tool_operations_support import build

PORT = [('reader.lookup', 'READER_TOOL_TIMEOUT', 0.5)]


def load(root, **kwargs):
    entry = build(root, 'older', **kwargs)
    return load_definition(entry, base_root=root, pack_root=root)


def test_root_row_names_a_node_port(tmp_path):
    definition = load(tmp_path, timeouts=[('GRAPH', 'GRAPH_TIMEOUT', 60), *PORT])
    assert [row['Owner'] for row in definition.layout.tables['Timeouts']] == ['GRAPH', 'reader.lookup']
    assert declared_timeouts(definition) == {'GRAPH': 60, 'reader.lookup': 0.5}


def test_node_document_row_names_its_own_port_with_the_full_owner(tmp_path):
    definition = load(tmp_path, node_timeouts={'reader': PORT})
    (row,) = definition.layout.tables['Timeouts']
    assert (row['Owner'], row['Key'], row['_owner_id']) == ('reader.lookup', 'READER_TOOL_TIMEOUT', 'reader')
    assert declared_timeouts(definition, {'READER_TOOL_TIMEOUT': 2}) == {'reader.lookup': 2}


@pytest.mark.parametrize('rows,message', [
    ([('ghost.lookup', 'KEY_ONE', 1)], r'flow\.md\.html:\d+: timeout port owner must name a node'),
    ([('work.lookup', 'KEY_ONE', 1)], r'flow\.md\.html:\d+: timeout port owner must name a node'),
    ([('reader.absent', 'KEY_ONE', 1)], r'flow\.md\.html:\d+: timeout port is not declared in the node Tools table'),
    ([('reader.store', 'KEY_ONE', 1)], r'flow\.md\.html:\d+: timeout port is not declared in the node Tools table'),
    ([('reader.lookup.more', 'KEY_ONE', 1)], r'timeout port is not declared'),
    ([('reader.lookup', 'KEY_ONE', 1), ('reader.lookup', 'KEY_TWO', 1)], r'flow\.md\.html:\d+: duplicate timeout port'),
    ([('reader.lookup', 'bad-key', 1)], r'flow\.md\.html:\d+: invalid timeout key'),
    ([('lookup', 'KEY_ONE', 1)], r'timeout owner/key'),
])
def test_invalid_root_port_rows_are_rejected_with_their_location(tmp_path, rows, message):
    with pytest.raises(LayoutError, match=message):
        load(tmp_path, timeouts=rows)


@pytest.mark.parametrize('node,rows,message', [
    ('reader', [('writer.store', 'KEY_ONE', 1)], r"reader\.md\.html:\d+: Owner must name the enclosing node 'reader'"),
    ('reader', [('lookup', 'KEY_ONE', 1)], r"reader\.md\.html:\d+: Owner must name the enclosing node 'reader'"),
    ('reader', [('reader.store', 'KEY_ONE', 1)], r'reader\.md\.html:\d+: timeout port is not declared'),
    ('reader', [('reader.lookup', 'KEY_ONE', 1), ('reader.lookup', 'KEY_TWO', 1)], r'reader\.md\.html:\d+: duplicate timeout port'),
])
def test_invalid_node_document_port_rows_are_rejected_with_their_location(tmp_path, node, rows, message):
    with pytest.raises(LayoutError, match=message):
        load(tmp_path, node_timeouts={node: rows})


def test_node_document_and_root_may_not_both_declare_the_port(tmp_path):
    with pytest.raises(LayoutError, match='duplicate timeout port'):
        load(tmp_path, timeouts=[('reader.lookup', 'KEY_ONE', 1)],
             node_timeouts={'reader': [('reader.lookup', 'KEY_TWO', 1)]})


def test_port_row_changes_declaration_and_workflow_identity(tmp_path):
    (tmp_path / 'first').mkdir()
    (tmp_path / 'second').mkdir()
    first = load(tmp_path / 'first', timeouts=PORT)
    second = load(tmp_path / 'second', timeouts=[('writer.store', 'READER_TOOL_TIMEOUT', 0.5)])
    assert declaration_identity(first) != declaration_identity(second)
    assert (workflow_identity(first, timeouts={'reader.lookup': 0.5}, component_passes={'work': 1})
            != workflow_identity(first, timeouts={'reader.lookup': 0.6}, component_passes={'work': 1}))
    with pytest.raises(ValueError, match='owner coverage'):
        workflow_identity(first, timeouts={}, component_passes={'work': 1})
