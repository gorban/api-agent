"""The viewer shows which nodes read and write declared session fields."""
from api_agent.workflows.viewer_projection import project_view
from api_agent.workflows.viewer_render import _label, _projection_source

from session_workflow_support import load_session, session_entry


def wrapped(*lines):
    return '\\n'.join(_label(line) for line in lines)


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

    assert wrapped('Session read:', 'history, draft_lines, last_text') in root
    assert wrapped('Session write:', 'history, topic, last_text') in root
    assert wrapped('Session read:', 'topic') in component
    assert wrapped('Session write:', 'draft_lines, last_text') in component
    assert root.count('"Session ') == 2 and component.count('"Session ') == 2
    assert wrapped('Session read:', 'topic') not in root


def test_turn_reads_are_not_drawn(tmp_path):
    from test_workflow_turn import FIXTURE
    projection = project_view(load_session(session_entry(
        tmp_path, fixture=FIXTURE, flow='turn_chat.md.html')))

    assert not [ref for ref in projection.references if ref.kind == 'turn']
    for path in ('turn_chat.md.html', 'refine.md.html'):
        assert 'Turn' not in diagram(projection, path)
    assert wrapped('Session read:', 'last_extra') in diagram(projection, 'turn_chat.md.html')


def test_long_session_field_lists_split_into_labels_within_the_limit():
    from api_agent.workflows.viewer_render import _MAX_LABEL, _session_labels
    fields = [f'retained_field_{index:02d}_with_a_long_descriptive_name' for index in range(20)]

    labels = _session_labels('read', fields)

    assert len(labels) > 1
    assert all(len(label) <= _MAX_LABEL and label.startswith('Session read: ') for label in labels)
    assert [field for label in labels for field in label.removeprefix('Session read: ').split(', ')] == fields
    assert _session_labels('write', ['topic']) == ['Session write: topic']


def test_session_boxes_wrap_three_fields_per_line_under_their_heading():
    from api_agent.workflows.viewer_render import _session_box_label
    label = _session_box_label('Session read: one, two, three, four, five')

    assert label == wrapped('Session read:', 'one, two, three', 'four, five')


def test_boxes_attached_to_one_node_hang_progressively_lower(tmp_path):
    import re
    projection = project_view(load_session(session_entry(tmp_path)))
    source = diagram(projection, 'chat.md.html')
    owner = re.search(r'rectangle "reply" as (n\d+)', source).group(1)
    links = re.findall(rf'^{owner} (\.+) (?:ref|session|timeout|limit)\d+$', source, re.MULTILINE)

    assert len(links) >= 3
    assert [len(dots) for dots in links] == list(range(2, len(links) + 2))
