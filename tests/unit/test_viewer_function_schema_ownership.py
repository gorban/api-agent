"""Functions references follow their declared node while keeping captured provenance."""

from hashlib import sha256
from pathlib import Path

import pytest

from api_agent.workflows.definition import Replacement, load_definition
from api_agent.workflows.viewer_projection import project_view
from api_agent.workflows.viewer_render import _projection_source


FUNCTIONS = ('\n## Functions\n\n| ID | Registry | Input | Output |\n'
             '| --- | --- | --- | --- |\n'
             '| inspect | inspect | [input](schema.json) | [output](schema.json) |\n')
NATIVE = ('\n## Native\n\n| Registry | Kind | Source |\n| --- | --- | --- |\n'
          '| inspect | deterministic | [code](worker.py) |\n')


def selected_graph(tmp_path, scope, inline, replacement):
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    base.mkdir()
    pack.mkdir()
    entry = base / 'flow.md.html'
    entry.write_text('# Workflow v2\n\n## Graph ID\n\n| ID |\n| --- |\n| sample |\n\n'
                     '## Flow\n\n1.  [component group](group.md.html)\n'
                     '## Components\n\n| ID | Max passes | Guard |\n| --- | --- | --- |\n| group | 1 | - |\n'
                     + (FUNCTIONS if scope == 'root' else ''))
    root = pack if replacement else base
    component = root / 'group.md.html'
    component.write_text('# Component: group\n\n## Flow\n\n1.  [node inspect]('
                         + ('#node-inspect' if inline else 'inspect.md.html') + ')\n'
                         + (FUNCTIONS if scope == 'component' else ''))
    node_text = '# Node: inspect\n' + NATIVE + (FUNCTIONS if scope == 'node' else '')
    if inline:
        component.write_text(component.read_text() + '\n' + node_text.replace('# Node:', '## Node:').replace('\n## Native', '\n### Native').replace('\n## Functions', '\n### Functions'))
        node = component
    else:
        node = root / 'inspect.md.html'
        node.write_text(node_text)
    for directory in (base, pack):
        (directory / 'worker.py').write_text('def inspect(inputs, context):\n    return {}\n')
        (directory / 'schema.json').write_text('{"type":"object"}')
    replacements = (Replacement('group', Path('group.md.html'), 'pack'),) if replacement else ()
    admitted = load_definition(entry, base_root=base, pack_root=pack, replacements=replacements)
    owner = {'root': entry, 'component': component, 'node': node}[scope]
    return admitted, owner


@pytest.mark.parametrize('scope', ['root', 'component', 'node'])
@pytest.mark.parametrize('inline', [False, True])
@pytest.mark.parametrize('replacement', [False, True])
def test_functions_follow_row_node_with_physical_provenance(tmp_path, scope, inline, replacement):
    admitted, owner = selected_graph(tmp_path, scope, inline, replacement)
    captured = owner.read_bytes()
    owner.write_text('changed after admission')

    projection = project_view(admitted)

    refs = [ref for ref in projection.references if ref.kind == 'functions']
    assert len(refs) == 2
    assert {ref.owner_id for ref in refs} == {'inspect'}
    root_id = 'base' if owner.is_relative_to(admitted.roots['base']) else 'pack'
    expected_owner = root_id + '/' + owner.name
    assert {ref.owner_path for ref in refs} == {expected_owner}
    assert {ref.line for ref in refs} == {
        next(i for i, line in enumerate(captured.decode().splitlines(), 1)
             if line.startswith('| inspect | inspect |'))}
    assert {ref.target_path for ref in refs} == {root_id + '/schema.json'}
    assert {ref.target_sha256 for ref in refs} == {sha256(b'{"type":"object"}').hexdigest()}
    document = next(doc for doc in projection.documents
                    if doc.logical_path == ('pack' if replacement else 'base') + '/group.md.html')
    source, links = _projection_source(projection, document)
    assert b'Input: schema&#46;json' in source
    assert b'Output: schema&#46;json' in source
    assert len([link for link in links if 'contracts/' in link]) == 2
