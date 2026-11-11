"""Declaration errors name the document that declares the offending row."""
import pytest

from api_agent.workflows import LayoutError
from api_agent.workflows import session as session_contract
from loop_support import edges, graph, limits, load, table
from session_workflow_support import load_session, session_entry

GROUP_EDGES = edges(('verify', 'draft', 'not verify.accepted'), ('verify', 'END', 'otherwise'))


def message(error):
    return str(error.value)


def test_an_unbounded_backward_edge_in_a_component_names_the_component_document(tmp_path):
    entry = graph(tmp_path, [('component', 'group'), ('node', 'accept')], sections=limits('PLAN_PASSES'),
                  components={'group': (['draft', 'verify'], GROUP_EDGES)})

    with pytest.raises(LayoutError, match='backward edge to draft requires a bounded When') as error:
        load(entry)

    assert 'group.md.html:' in message(error) and 'flow.md.html' not in message(error)


def test_an_unbounded_backward_edge_in_the_root_names_the_root_document(tmp_path):
    entry = graph(tmp_path, [('node', 'draft'), ('node', 'verify')],
                  sections=limits('PLAN_PASSES') + edges(('verify', 'draft', 'not verify.accepted'),
                                                         ('verify', 'END', 'otherwise')))

    with pytest.raises(LayoutError, match='bounded When') as error:
        load(entry)

    assert 'flow.md.html:' in message(error)


def node_binding(tmp_path, row):
    """Declare ``row`` in verify's own document, in place of its draft_n binding."""
    entry = graph(tmp_path, [('node', 'draft'), ('node', 'verify')])
    path = tmp_path / 'verify.md.html'
    text = path.read_text().replace('| verify | draft_n | draft | n | - |\n', '')
    path.write_text(text.replace('| verify | accept_at | START | accept_at | - |',
                                 '| verify | accept_at | START | accept_at | - |\n' + row))
    return entry


@pytest.mark.parametrize('row,reason', [
    ('| verify | draft_n | TURN | nope | - |', 'no node declares turn field'),
    ('| verify | draft_n | TURN | * | - |', 'TURN requires a named field'),
    ('| verify | draft_n | SESSION | nope | - |', 'undeclared session field'),
])
def test_a_session_or_turn_binding_in_a_node_document_names_that_document(tmp_path, row, reason):
    with pytest.raises(LayoutError, match=reason) as error:
        load(node_binding(tmp_path, row))

    assert 'verify.md.html:' in message(error) and 'flow.md.html' not in message(error)


def test_a_session_row_error_names_the_root_document(tmp_path):
    entry = session_entry(tmp_path, ('| topic | reply.topic | replace |', '| topic | reply.nothing | replace |'))

    with pytest.raises(LayoutError, match='unknown session source output field') as error:
        load_session(entry)

    assert 'chat.md.html:' in message(error)


def test_the_session_check_returns_the_offending_row(tmp_path):
    definition = load_session(session_entry(tmp_path))
    tables = dict(definition.layout.tables)
    tables['Session'] = ()
    assert session_contract.problem(tables) == ('Session requires at least one row', {'line': 1})
    tables['Session'] = (dict(tables['Bindings'][0], Field='x', Source='nope', Merge='replace'),)

    reason, row = session_contract.problem(tables)

    assert reason == 'invalid session source; use node.field' and row is tables['Session'][0]


def test_an_edge_row_in_a_component_document_names_that_document(tmp_path):
    entry = graph(tmp_path, [('component', 'group'), ('node', 'accept')],
                  components={'group': (['draft', 'verify'], edges(('verify', 'accept', 'always')))})

    with pytest.raises(LayoutError, match='edges must connect siblings or their END') as error:
        load(entry)

    assert 'group.md.html:' in message(error) and 'flow.md.html' not in message(error)


def test_a_component_row_in_its_own_document_names_that_document(tmp_path):
    rows = table('Components', ('ID', 'Max passes', 'Guard'), [('group', '2', 'draft')])
    entry = graph(tmp_path, [('component', 'group'), ('node', 'accept')],
                  components={'group': (['draft', 'verify'], rows)})

    with pytest.raises(LayoutError, match='Guard must be the final directly owned step') as error:
        load(entry)

    assert 'group.md.html:' in message(error) and 'flow.md.html' not in message(error)


def test_a_binding_row_in_a_node_document_names_that_document(tmp_path):
    entry = node_binding(tmp_path, '| verify | nope | START | accept_at | - |')

    with pytest.raises(LayoutError, match='unknown or duplicate input binding') as error:
        load(entry)

    assert 'verify.md.html:' in message(error) and 'flow.md.html' not in message(error)


def test_a_functions_row_in_a_node_document_names_that_document(tmp_path):
    entry = graph(tmp_path, [('node', 'draft'), ('node', 'verify')])
    (tmp_path / 'verify_out.json').write_text('{"type": "array"}')

    with pytest.raises(LayoutError, match='schemas must be object contracts') as error:
        load(entry)

    assert 'verify.md.html:' in message(error) and 'flow.md.html' not in message(error)


def test_an_unknown_inline_prompt_in_a_node_document_names_that_document(tmp_path):
    entry = graph(tmp_path, [('node', 'draft'), ('node', 'verify')])
    path = tmp_path / 'verify.md.html'
    path.write_text(path.read_text() + table('Prompts', ('Node', 'File', 'When'), [('verify', '@missing', 'always')]))

    with pytest.raises(LayoutError, match='unknown inline prompt') as error:
        load(entry)

    assert 'verify.md.html:' in message(error) and 'flow.md.html' not in message(error)
