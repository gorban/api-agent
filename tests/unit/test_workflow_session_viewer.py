"""The viewer shows which nodes read and write declared session fields."""
from api_agent.workflows.viewer_projection import project_view
from api_agent.workflows.viewer_render import _label, _projection_source

from session_workflow_support import load_session, session_entry


def diagram(projection, path):
    document = next(item for item in projection.documents if item.logical_path.endswith(path))
    return _projection_source(projection, document)[0].decode()


def test_projection_records_session_reads_and_writes_per_node(tmp_path):
    projection = project_view(load_session(session_entry(tmp_path)))

    accesses = {(ref.owner_id, dict(ref.attributes)['access'], dict(ref.attributes)['field'])
                for ref in projection.references if ref.kind == 'session'}

    assert accesses == {
        ('reply', 'read', 'history'), ('reply', 'read', 'draft_lines'), ('reply', 'read', 'last_text'),
        ('draft', 'read', 'topic'),
        ('reply', 'write', 'history'), ('reply', 'write', 'topic'), ('reply', 'write', 'last_text'),
        ('draft', 'write', 'draft_lines'), ('draft', 'write', 'last_text')}


def test_diagrams_show_a_session_note_on_the_reading_and_writing_node(tmp_path):
    projection = project_view(load_session(session_entry(tmp_path)))

    root = diagram(projection, 'chat.md.html')
    component = diagram(projection, 'refine.md.html')

    assert _label('Session read: history') in root
    assert _label('Session write: topic') in root
    assert _label('Session write: draft_lines') in component
    assert _label('Session read: topic') in component
    assert _label('Session read: draft_lines') in root
    assert _label('Session write: draft_lines') not in root


def test_diagram_notes_which_nodes_read_turn_values(tmp_path):
    from test_workflow_turn import FIXTURE
    projection = project_view(load_session(session_entry(
        tmp_path, fixture=FIXTURE, flow='turn_chat.md.html')))

    reads = {(ref.owner_id, dict(ref.attributes)['field'])
             for ref in projection.references if ref.kind == 'turn'}

    assert reads == {('path_a', 'topic'), ('path_b', 'topic'), ('draft', 'topic'), ('draft', 'hint'),
                     ('draft', 'scratch'), ('finish', 'topic'), ('finish', 'extra'),
                     ('finish', 'scratch'), ('finish', 'hint')}
    assert _label('Turn read: topic') in diagram(projection, 'turn_chat.md.html')
    assert _label('Turn read: scratch') in diagram(projection, 'refine.md.html')
    assert _label('Session read: last_extra') in diagram(projection, 'turn_chat.md.html')
