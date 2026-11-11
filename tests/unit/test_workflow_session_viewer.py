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


def test_diagrams_show_one_compact_session_box_per_node_and_access(tmp_path):
    projection = project_view(load_session(session_entry(tmp_path)))

    root = diagram(projection, 'chat.md.html')
    component = diagram(projection, 'refine.md.html')

    assert _label('Session read: history, draft_lines, last_text') in root
    assert _label('Session write: history, topic, last_text') in root
    assert _label('Session read: topic') in component
    assert _label('Session write: draft_lines, last_text') in component
    assert root.count('"Session ') == 2 and component.count('"Session ') == 2
    assert 'Session read: topic' not in root and 'Session write: history' not in component


def test_turn_reads_are_not_drawn(tmp_path):
    from test_workflow_turn import FIXTURE
    projection = project_view(load_session(session_entry(
        tmp_path, fixture=FIXTURE, flow='turn_chat.md.html')))

    assert not [ref for ref in projection.references if ref.kind == 'turn']
    for path in ('turn_chat.md.html', 'refine.md.html'):
        assert 'Turn' not in diagram(projection, path)
    assert _label('Session read: last_extra') in diagram(projection, 'turn_chat.md.html')
