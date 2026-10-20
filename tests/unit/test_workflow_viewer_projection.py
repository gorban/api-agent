"""The static view follows only the admitted selected graph."""

from pathlib import Path
from dataclasses import replace
import shutil
from types import MappingProxyType

import pytest

from api_agent.workflows.definition import Replacement, load_definition
from api_agent.workflows.document_format import (canonical_executable_bytes, extract_document,
                                                 validate_presentation_location, wrap_document)
from api_agent.workflows.layout import LayoutError
from api_agent.workflows.execution import diagram_layout
from api_agent.workflows.stateful import diagram_stateful, load_stateful_definition
from api_agent.workflows.viewer_projection import project_view


def graph(root):
    (root / 'children').mkdir()
    (root / 'native.py').write_text('def work(inputs, context):\n    return {}\n')
    (root / 'schema.json').write_text('{"type":"object"}')
    entry = root / 'flow.md.html'
    entry.write_text('# Workflow v2\n\n## Graph ID\n\n| ID |\n| --- |\n| example |\n\n'
                     '## Flow\n\n1.  [node work](children/work.md.html)\n')
    (root / 'children' / 'work.md.html').write_text(
        '# Node v2: work\n\n## Native\n\n| Registry | Kind | Source |\n| --- | --- | --- |\n'
        '| work | deterministic | [Python](../native.py) |\n\n'
        '## Functions\n\n| ID | Registry | Input | Output |\n| --- | --- | --- | --- |\n'
        '| work | work | [input](../schema.json) | [output](../schema.json) |\n\n'
        '## Prompt: literal\n\n```text\n## Generated Diagram v1\n'
        '<script>this is prompt text</script>\n</plaintext>\n<!-- workflow-generated-diagram-v1:end -->\n```\n\n'
        '## Prompts\n\n| Node | File | When |\n| --- | --- | --- |\n'
        '| work | @literal | always |\n')
    return entry


def admit(entry):
    return load_definition(entry, base_root=entry.parent, pack_root=entry.parent)


def test_presented_documents_keep_semantics_and_original_line_locations(tmp_path):
    entry = graph(tmp_path)
    bare = admit(entry)
    bare_view = project_view(bare)
    bare_diagram = diagram_layout(bare.layout)
    original = entry.read_bytes()
    entry.write_bytes(wrap_document(original, root=True, asset_prefix='.private_resources/viewer/',
                                    generated=(b'![Workflow diagram](.private_resources/diagrams/flow.svg)\n\n'
                                               b'[Open standalone diagram](.private_resources/diagrams/flow.svg)')))
    child = tmp_path / 'children' / 'work.md.html'
    child_original = child.read_bytes()
    child.write_bytes(wrap_document(child_original, root=False,
                                    asset_prefix='../.private_resources/viewer/'))

    selected = admit(entry)
    view = project_view(selected)

    assert [(step.id, step.parent) for step in selected.layout.steps] == [
        (step.id, step.parent) for step in bare.layout.steps]
    assert selected.layout.tables['Prompts'][0]['File'].id == 'literal'
    assert selected.contents[selected.layout.tables['Prompts'][0]['File']] == (
        b'## Generated Diagram v1\n<script>this is prompt text</script>\n</plaintext>\n'
        b'<!-- workflow-generated-diagram-v1:end -->\n')
    root_lines = extract_document(entry.read_bytes(), path=entry).line_map
    assert view.steps[0].flow_line == root_lines[bare_view.steps[0].flow_line - 1]
    assert view.documents[0].executable_bytes == original
    assert view.documents[1].executable_bytes == child_original
    assert view.definition_id != bare_view.definition_id
    assert view.projection_sha256 == bare_view.projection_sha256
    assert view.edges == bare_view.edges
    assert diagram_layout(selected.layout) == bare_diagram
    assert view.references[0].target_sha256 == bare_view.references[0].target_sha256


@pytest.mark.parametrize('corruption', [
    lambda raw: raw.replace(b'content="v1"', b'content="v2"'),
    lambda raw: raw.replace(b'viewer.js', b'https://elsewhere.invalid/viewer.js'),
    lambda raw: raw.replace(b'<plaintext>', b'<div>'),
    lambda raw: raw + b'<script>bad</script>',
])
def test_envelope_rejects_alteration_or_executable_markup(tmp_path, corruption):
    entry = graph(tmp_path)
    wrapped = wrap_document(entry.read_bytes(), root=True, asset_prefix='.private_resources/viewer/')
    entry.write_bytes(corruption(wrapped))

    with pytest.raises(LayoutError):
        admit(entry)


def test_generated_marker_must_be_terminal_and_exact(tmp_path):
    entry = graph(tmp_path)
    wrapped = wrap_document(entry.read_bytes(), root=True, asset_prefix='.private_resources/viewer/',
                            generated=(b'![Workflow diagram](.private_resources/diagrams/flow.svg)\n\n'
                                       b'[Open standalone diagram](.private_resources/diagrams/flow.svg)'))
    entry.write_bytes(wrapped.replace(b'workflow-generated-diagram-v1:end',
                                      b'workflow-generated-diagram-v1:changed'))

    with pytest.raises(LayoutError):
        admit(entry)


def test_generated_section_cannot_hide_executable_table(tmp_path, monkeypatch):
    entry = graph(tmp_path)
    body = (b'![Workflow diagram](.private_resources/diagrams/flow.svg)\n\n'
            b'[Open standalone diagram](.private_resources/diagrams/flow.svg)\n\n'
            b'## Flow\n\n1.  [node hidden](hidden.md.html)')
    entry.write_bytes(wrap_document(entry.read_bytes(), root=True,
                                    asset_prefix='.private_resources/viewer/', generated=body))

    with pytest.raises(LayoutError, match='generated diagram'):
        admit(entry)
    # Focused mutation: disabling only the finite generated-body guard makes
    # the same document admissible, so the rejection above is causally tested.
    monkeypatch.setattr('api_agent.workflows.document_format._GENERATED_BODY',
                        type('PermissiveMatch', (), {'fullmatch': lambda self, value: object()})())
    assert admit(entry).graph_id == 'example'


def test_presentation_asset_and_diagram_paths_are_owned(tmp_path):
    entry = graph(tmp_path)
    child = tmp_path / 'children' / 'work.md.html'
    child.write_bytes(wrap_document(child.read_bytes(), root=False,
                                    asset_prefix='.private_resources/viewer/'))
    with pytest.raises(LayoutError, match='asset prefix'):
        admit(entry)

    child.write_bytes(wrap_document(extract_document(child.read_bytes(), path=child).executable_bytes,
                                    root=False, asset_prefix='../.private_resources/viewer/',
                                    generated=(b'![Workflow diagram](../.private_resources/diagrams/wrong.svg)\n\n'
                                               b'[Open standalone diagram](../.private_resources/diagrams/wrong.svg)')))
    with pytest.raises(LayoutError, match='diagram path'):
        admit(entry)


def test_generated_navigation_cannot_escape_owner_root(tmp_path):
    entry = graph(tmp_path)
    body = (b'![Workflow diagram](.private_resources/diagrams/flow.svg)\n\n'
            b'[Open standalone diagram](.private_resources/diagrams/flow.svg)\n\n'
            b'- [work](../../outside.md.html)')
    entry.write_bytes(wrap_document(entry.read_bytes(), root=True,
                                    asset_prefix='.private_resources/viewer/', generated=body))

    with pytest.raises(LayoutError, match='navigation escapes'):
        admit(entry)


def test_generated_heading_needs_exact_blank_line_and_multiple_safe_links(tmp_path):
    entry = graph(tmp_path)
    raw = entry.read_bytes()
    body = (b'![Workflow diagram](.private_resources/diagrams/flow.svg)\n\n'
            b'[Open standalone diagram](.private_resources/diagrams/flow.svg)\n\n'
            b'- [first](children/first.md.html)\n- [second](children/second.md.html)')
    presented = wrap_document(raw, root=True, asset_prefix='.private_resources/viewer/',
                              generated=body)
    assert extract_document(presented, path=entry).executable_bytes == raw
    entry.write_bytes(presented)
    assert admit(entry).graph_id == 'example'
    malformed = presented.replace(b'\n\n## Generated Diagram v1', b'## Generated Diagram v1')
    with pytest.raises(LayoutError, match='generated diagram'):
        extract_document(malformed, path=entry)


def test_root_fallback_is_exact_and_nested_document_has_bootstrap_only(tmp_path):
    entry = graph(tmp_path)
    root = wrap_document(entry.read_bytes(), root=True,
                         asset_prefix='.private_resources/viewer/')
    child = tmp_path / 'children' / 'work.md.html'
    nested = wrap_document(child.read_bytes(), root=False,
                           asset_prefix='../.private_resources/viewer/')

    assert b'<h2>Viewing this file</h2>' in root
    assert b'Python 3.13 or newer' in root
    assert b'\n\n\n\n\n\n<!--\n  Open this workflow' in root
    assert b'./launch.py --view-flow\n-->\n\n\n\n\n\n<section' in root
    assert b'<h2>Viewing this file</h2>' not in nested
    assert extract_document(root, path=entry).executable_bytes == entry.read_bytes()
    assert extract_document(nested, path=child).executable_bytes == child.read_bytes()
    from api_agent.workflows import document_format
    entry.write_bytes(root.replace(document_format._ROOT_FALLBACK, b''))
    with pytest.raises(LayoutError, match='asset prefix'):
        admit(entry)


def test_generated_navigation_accepts_uri_escaped_names_within_owner(tmp_path):
    (tmp_path / 'children').mkdir()
    child = tmp_path / 'children' / 'my node.md.html'
    executable = b'# Node v2: work\n'
    svg = b'../.private_resources/diagrams/children/my%20node.svg'
    body = (b'![Workflow diagram](' + svg + b')\n\n'
            b'[Open standalone diagram](' + svg + b')\n\n'
            b'- [work](other%20node.md.html)')
    wrapped = wrap_document(executable, root=False,
                            asset_prefix='../.private_resources/viewer/', generated=body)

    validate_presentation_location(wrapped, path=child, root=tmp_path)
    assert extract_document(wrapped, path=child).executable_bytes == executable


@pytest.mark.parametrize('target', [b'..%2foutside.md.html', b'%2e%2e/outside.md.html',
                                      b'bad%00name.md.html', b'%FF.md.html',
                                      b'../../outside.md.html'])
def test_generated_navigation_rejects_encoded_or_decoded_escape(tmp_path, target):
    entry = graph(tmp_path)
    svg = b'.private_resources/diagrams/flow.svg'
    body = (b'![Workflow diagram](' + svg + b')\n\n'
            b'[Open standalone diagram](' + svg + b')\n\n'
            b'- [work](' + target + b')')
    wrapped = wrap_document(entry.read_bytes(), root=True,
                            asset_prefix='.private_resources/viewer/', generated=body)

    with pytest.raises(LayoutError):
        validate_presentation_location(wrapped, path=entry, root=tmp_path)


def test_projection_rejects_foreign_owner_and_missing_captured_reference(tmp_path):
    entry = graph(tmp_path)
    admitted = admit(entry)
    outside = tmp_path.parent / 'outside.md.html'
    selected = MappingProxyType({'work': (outside, 11, entry.parent / 'children/work.md.html')})
    with pytest.raises(ValueError, match='outside admitted roots'):
        project_view(replace(admitted, selected_declarations=selected))
    row = dict(admitted.layout.tables['Functions'][0])
    row['Input'] = tmp_path / 'unadmitted.json'
    tables = dict(admitted.layout.tables)
    tables['Functions'] = (row,)
    forged = replace(admitted, layout=replace(admitted.layout, tables=tables))
    with pytest.raises(ValueError, match='missing captured viewer reference'):
        project_view(forged)


def test_selected_override_ignores_unselected_original_and_relocates(tmp_path):
    first = tmp_path / 'first'
    first.mkdir()
    entry = graph(first)
    original = first / 'children' / 'work.md.html'
    alternate = first / 'children' / 'selected.md.html'
    alternate.write_bytes(original.read_bytes())
    selected = (Replacement('work', Path('children/selected.md.html'), 'pack'),)
    before = project_view(load_definition(entry, base_root=first, pack_root=first,
                                          replacements=selected))
    original.write_text('changed and unselected')
    after = project_view(load_definition(entry, base_root=first, pack_root=first,
                                         replacements=selected))
    original.unlink()
    deleted = project_view(load_definition(entry, base_root=first, pack_root=first,
                                           replacements=selected))
    second = tmp_path / 'second'
    shutil.copytree(first, second)
    relocated = project_view(load_definition(second / 'flow.md.html', base_root=second,
                                             pack_root=second, replacements=selected))

    assert before == after == deleted == relocated
    assert before.steps[0].declaration_path == 'pack/children/selected.md.html'
    assert all('work.md.html' not in document.logical_path for document in before.documents)


def test_projection_uses_frozen_bytes_after_source_changes(tmp_path, monkeypatch):
    entry = graph(tmp_path)
    admitted = admit(entry)
    expected = project_view(admitted)
    (tmp_path / 'children' / 'work.md.html').write_text('changed after admission')
    (tmp_path / 'native.py').unlink()
    monkeypatch.setattr('api_agent.workflows.definition._read_bounded',
                        lambda *args: pytest.fail('projection reread filesystem'))

    assert project_view(admitted) == expected


def test_semantic_content_changes_digest_and_routes(tmp_path):
    entry = graph(tmp_path)
    before = project_view(admit(entry))
    child = tmp_path / 'children' / 'work.md.html'
    child.write_text(child.read_text().replace('When |\n| --- | --- | --- |\n| work | @literal | always',
                                              'When |\n| --- | --- | --- |\n| work | @literal | conditional'))
    after = project_view(admit(entry))

    assert after.definition_id != before.definition_id
    assert after.projection_sha256 != before.projection_sha256
    assert after.references != before.references


def test_extractor_preserves_fenced_marker_and_rejects_bad_asset_prefix(tmp_path):
    entry = graph(tmp_path)
    original = entry.read_bytes()
    extracted = extract_document(original, path=entry)

    assert extracted.executable_bytes == original
    assert extracted.generated is None
    assert canonical_executable_bytes(original, path=entry) == original
    with pytest.raises(LayoutError, match='asset prefix'):
        wrap_document(original, root=True, asset_prefix='../../evil/')


def test_v3_presented_graph_has_selected_routes_and_frozen_state_reference(tmp_path):
    (tmp_path / 'native.py').write_text(
        'from typing import TypedDict\nclass State(TypedDict):\n    seen: bool\n'
        'def work(state, context):\n    return {"seen": True}\n')
    (tmp_path / 'work.md.html').write_text(
        '# Node v3: work\n\n## Native\n\n| Registry | Kind | Source |\n'
        '| --- | --- | --- |\n| work | deterministic | [Python](native.py) |\n')
    entry = tmp_path / 'flow.md.html'
    entry.write_text('# Workflow v3\n\n## Graph ID\n\n| ID |\n| --- |\n| state_graph |\n\n'
                     '## State\n\n| Registry | Source |\n| --- | --- |\n| State | [Python](native.py) |\n\n'
                     '## Flow\n\n1.  [node work](work.md.html)\n\n## Edges\n\n'
                     '| From | To | Router | Source |\n| --- | --- | --- | --- |\n'
                     '| work | END | - | - |\n')
    (tmp_path / 'limits.toml').write_text(
        'version = 1\n[fields.GRAPH_LIMIT]\ntype = "float"\ndefault = 1.0\n')
    (tmp_path / 'prompt.txt').write_text('Admitted prompt.\n')
    entry.write_text(entry.read_text() + '\n## Resources\n\n| ID | Kind | Source |\n'
                     '| --- | --- | --- |\n| greeting | prompt | [text](prompt.txt) |\n\n'
                     '## Timeouts\n\n| Owner | Source | Key |\n| --- | --- | --- |\n'
                     '| GRAPH | [config](limits.toml) | GRAPH_LIMIT |\n')
    bare = load_stateful_definition(entry, base_root=tmp_path, pack_root=tmp_path)
    bare_view = project_view(bare)
    bare_diagram = diagram_stateful(bare)
    entry.write_bytes(wrap_document(entry.read_bytes(), root=True,
                                    asset_prefix='.private_resources/viewer/'))
    selected = load_stateful_definition(entry, base_root=tmp_path, pack_root=tmp_path)
    view = project_view(selected)

    assert [(edge.source, edge.target, edge.implicit) for edge in view.edges] == [
        ('START', 'work', True), ('work', 'END', False)]
    assert view.edges == bare_view.edges
    assert diagram_stateful(selected) == bare_diagram
    root_lines = extract_document(entry.read_bytes(), path=entry).line_map
    assert view.steps[0].flow_line == root_lines[bare_view.steps[0].flow_line - 1]
    assert any(ref.kind == 'state_schema' and ref.target_path == 'base/native.py'
               for ref in view.references)
    assert any(ref.kind == 'timeout' and ref.target_path == 'base/limits.toml'
               for ref in view.references)
    assert any(ref.kind == 'prompt' and ref.target_path == 'base/prompt.txt'
               for ref in view.references)
    assert view.definition_id != bare_view.definition_id
    assert view.projection_sha256 == bare_view.projection_sha256


def nested_v3_graph(root, *, original='group.md.html', routed=False):
    (root / 'native.py').write_text(
        'from typing import TypedDict\nclass State(TypedDict):\n    seen: bool\n')
    (root / 'work.md.html').write_text(
        '# Node v3: work\n\n## Native\n\n| Registry | Kind | Source |\n'
        '| --- | --- | --- |\n| work | deterministic | [Python](native.py) |\n')
    (root / 'group.md.html').write_text(
        '# Component v3: group\n\n## Flow\n\n1.  [node work](work.md.html)\n')
    entry = root / 'flow.md.html'
    entry.write_text(
        '# Workflow v3\n\n## Graph ID\n\n| ID |\n| --- |\n| example |\n\n'
        '## State\n\n| Registry | Source |\n| --- | --- |\n| State | [Python](native.py) |\n\n'
        f'## Flow\n\n1.  [component group]({original})\n\n'
        '## Edges\n\n| From | To | Router | Source |\n| --- | --- | --- | --- |\n'
        '| group | END | - | - |\n'
        + ('| work | END | choose | [Python](native.py) |\n' if routed else
           '| work | END | - | - |\n'))
    return entry


def test_nested_v3_projection_has_component_entry_and_backlink(tmp_path):
    entry = nested_v3_graph(tmp_path)

    view = project_view(load_stateful_definition(entry, base_root=tmp_path, pack_root=tmp_path))

    assert ('group', 'START', 'work', True) in {
        (edge.owner, edge.source, edge.target, edge.implicit) for edge in view.edges}
    assert ('group', 'START', 'work', True) in {
        (edge.owner, edge.source, edge.target, edge.implicit) for edge in view.backlinks}


def test_v3_projection_binds_router_source_once_to_all_targets(tmp_path):
    entry = nested_v3_graph(tmp_path, routed=True)
    (tmp_path / 'work_two.md.html').write_text(
        '# Node v3: work_two\n\n## Native\n\n| Registry | Kind | Source |\n'
        '| --- | --- | --- |\n| work_two | deterministic | [Python](native.py) |\n')
    group = tmp_path / 'group.md.html'
    group.write_text(group.read_text() + '2.  [node work_two](work_two.md.html)\n')
    entry.write_text(entry.read_text().replace('| work | END | choose | [Python](native.py) |',
                                               '| work | work_two | choose | [Python](native.py) |\n'
                                               '| work | END | choose | [Python](native.py) |\n'
                                               '| work_two | END | - | - |'))

    view = project_view(load_stateful_definition(entry, base_root=tmp_path, pack_root=tmp_path))
    routers = [ref for ref in view.references if ref.kind == 'router_source']

    assert len(routers) == 1
    assert routers[0].owner_id == 'work'
    assert routers[0].target_path == 'base/native.py'
    assert ('router', 'choose') in routers[0].attributes
    assert ('targets', 'work_two,END') in routers[0].attributes


@pytest.mark.parametrize('original', ['../../outside.md.html', '/absolute.md.html',
                                      'https://example.invalid/work.md.html',
                                      '%2e%2e/outside.md.html', '..\\outside.md.html',
                                      '%00.md.html', '%01.md.html', '%1F.md.html',
                                      '%7F.md.html', '%FF.md.html', '%ZZ.md.html',
                                      '%A.md.html', 'bad%.md.html',
                                      'group.md.html?hidden', 'group.md.html#hidden',
                                      '../outside.txt'])
def test_v3_replacement_validates_original_link_without_reading_it(tmp_path, original, monkeypatch):
    entry = nested_v3_graph(tmp_path, original=original)
    selected = (Replacement('group', Path('group.md.html'), 'base'),)
    original_read = __import__('api_agent.workflows.stateful', fromlist=['_read_bounded'])._read_bounded
    def rejecting_original(path, root):
        if Path(path).name == 'outside.md.html':
            pytest.fail('unselected original was read')
        return original_read(path, root)
    monkeypatch.setattr('api_agent.workflows.stateful._read_bounded', rejecting_original)

    with pytest.raises(LayoutError, match='declaration link'):
        load_stateful_definition(entry, base_root=tmp_path, pack_root=tmp_path,
                                 replacements=selected)


def test_v3_replacement_rejects_existing_symlink_alias_without_reading(tmp_path, monkeypatch):
    outside = tmp_path.parent / 'outside.md.html'
    outside.write_text('do not read')
    (tmp_path / 'alias.md.html').symlink_to(outside)
    entry = nested_v3_graph(tmp_path, original='alias.md.html')
    selected = (Replacement('group', Path('group.md.html'), 'base'),)
    original_read = __import__('api_agent.workflows.stateful', fromlist=['_read_bounded'])._read_bounded
    def rejecting_original(path, root):
        if Path(path) == outside:
            pytest.fail('unselected original bytes were read')
        return original_read(path, root)
    monkeypatch.setattr('api_agent.workflows.stateful._read_bounded', rejecting_original)

    with pytest.raises(LayoutError, match='declaration link'):
        load_stateful_definition(entry, base_root=tmp_path, pack_root=tmp_path,
                                 replacements=selected)


def test_v3_replacement_reports_malformed_url_authority(tmp_path):
    entry = nested_v3_graph(tmp_path, original='//[broken.md.html')
    selected = (Replacement('group', Path('group.md.html'), 'base'),)

    with pytest.raises(LayoutError, match='invalid declaration link'):
        load_stateful_definition(entry, base_root=tmp_path, pack_root=tmp_path,
                                 replacements=selected)


def test_v3_replacement_reports_original_resolution_failure(tmp_path, monkeypatch):
    entry = nested_v3_graph(tmp_path, original='unavailable.md.html')
    selected = (Replacement('group', Path('group.md.html'), 'base'),)
    original_resolve = Path.resolve

    def unavailable_original(path, *args, **kwargs):
        if path == tmp_path / 'unavailable.md.html':
            raise OSError('filesystem lookup failed')
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'resolve', unavailable_original)

    with pytest.raises(LayoutError, match='invalid declaration link'):
        load_stateful_definition(entry, base_root=tmp_path, pack_root=tmp_path,
                                 replacements=selected)


def test_v3_replacement_accepts_valid_uri_encoded_missing_original(tmp_path):
    entry = nested_v3_graph(tmp_path, original='missing%20original.md.html')
    selected = (Replacement('group', Path('group.md.html'), 'base'),)

    definition = load_stateful_definition(entry, base_root=tmp_path, pack_root=tmp_path,
                                          replacements=selected)

    assert definition.layout.steps[0].id == 'group'
    assert definition.selected_declarations['group'][2] == tmp_path / 'group.md.html'


def test_invalid_percent_rejection_depends_on_percent_syntax_guard(tmp_path, monkeypatch):
    entry = nested_v3_graph(tmp_path, original='%ZZ.md.html')
    selected = (Replacement('group', Path('group.md.html'), 'base'),)
    with pytest.raises(LayoutError, match='declaration link'):
        load_stateful_definition(entry, base_root=tmp_path, pack_root=tmp_path,
                                 replacements=selected)
    import re
    original_search = re.search
    def without_percent_syntax_check(pattern, value, flags=0):
        if pattern == r'%(?![0-9A-Fa-f]{2})':
            return None
        return original_search(pattern, value, flags)
    with monkeypatch.context() as mutation:
        mutation.setattr('api_agent.workflows.stateful.re.search', without_percent_syntax_check)
        assert load_stateful_definition(entry, base_root=tmp_path, pack_root=tmp_path,
                                        replacements=selected).graph_id == 'example'


def test_explicit_pack_alias_preserves_one_owner_and_semantic_digest(tmp_path):
    entry = nested_v3_graph(tmp_path)
    bare = project_view(load_stateful_definition(entry, base_root=tmp_path, pack_root=tmp_path,
                                                 entry_root='pack'))
    entry.write_bytes(wrap_document(entry.read_bytes(), root=True,
                                    asset_prefix='.private_resources/viewer/'))
    presented = project_view(load_stateful_definition(entry, base_root=tmp_path, pack_root=tmp_path,
                                                      entry_root='pack'))

    assert presented.definition_id != bare.definition_id
    assert presented.projection_sha256 == bare.projection_sha256
    assert all(document.logical_path.startswith('pack/') for document in presented.documents)
    assert all(resource.logical_path.startswith('pack/') for resource in presented.resources)


@pytest.mark.parametrize('version', [2, 3])
def test_crlf_root_can_wrap_without_executable_byte_change(tmp_path, version):
    if version == 2:
        entry = graph(tmp_path)
        loader = load_definition
    else:
        entry = nested_v3_graph(tmp_path)
        loader = load_stateful_definition
    raw = entry.read_bytes().replace(b'\n', b'\r\n')
    entry.write_bytes(raw)
    before = project_view(loader(entry, base_root=tmp_path, pack_root=tmp_path))
    wrapped = wrap_document(raw, root=True, asset_prefix='.private_resources/viewer/')
    entry.write_bytes(wrapped)

    after = project_view(loader(entry, base_root=tmp_path, pack_root=tmp_path))

    assert extract_document(wrapped, path=entry).executable_bytes == raw
    assert after.documents[0].executable_bytes == raw
    assert after.documents[0].sha256 == before.documents[0].sha256
    assert after.projection_sha256 == before.projection_sha256
    assert after.definition_id != before.definition_id


def test_crlf_wrap_depends_on_shared_root_detection(tmp_path, monkeypatch):
    entry = nested_v3_graph(tmp_path)
    raw = entry.read_bytes().replace(b'\n', b'\r\n')
    assert wrap_document(raw, root=True, asset_prefix='.private_resources/viewer/')
    with monkeypatch.context() as mutation:
        mutation.setattr('api_agent.workflows.document_format._is_root_document',
                         lambda data: data.startswith(b'# Workflow v3\n'))
        with pytest.raises(LayoutError, match='root mismatch'):
            wrap_document(raw, root=True, asset_prefix='.private_resources/viewer/')


def test_document_format_guards_invalid_inputs_and_reserved_heading(tmp_path):
    entry = graph(tmp_path)
    raw = entry.read_bytes()
    with pytest.raises(TypeError):
        extract_document('not bytes', path=entry)
    with pytest.raises(TypeError):
        wrap_document(raw, root=True, asset_prefix='.private_resources/viewer/', generated='not bytes')
    with pytest.raises(LayoutError, match='executable Markdown'):
        wrap_document(wrap_document(raw, root=True, asset_prefix='.private_resources/viewer/'),
                      root=True, asset_prefix='.private_resources/viewer/')
    with pytest.raises(LayoutError, match='root mismatch'):
        wrap_document(raw, root=False, asset_prefix='.private_resources/viewer/')
    with pytest.raises(LayoutError, match='envelope'):
        extract_document(b'<!DOCTYPE html>\n' + raw, path=entry)
    with pytest.raises(LayoutError, match='UTF-8'):
        extract_document(b'\xff', path=entry)
    with pytest.raises(LayoutError, match='generated diagram'):
        extract_document(raw + b'\n## Generated Diagram v1\n', path=entry)
    with pytest.raises(LayoutError, match='generated diagram'):
        extract_document(raw + b'\n\n## Generated Diagram v1\n'
                         b'![Workflow diagram](.private_resources/diagrams/flow.svg)\n\n'
                         b'[Open standalone diagram](.private_resources/diagrams/flow.svg)\n'
                         b'## Bindings\n'
                         b'<!-- workflow-generated-diagram-v1:end -->\n', path=entry)


def test_projection_rejects_missing_selection_and_maps_explicit_edge_and_binding(tmp_path):
    entry = graph(tmp_path)
    with pytest.raises(ValueError, match='admitted selected'):
        project_view(None)
    child = tmp_path / 'children' / 'work.md.html'
    child.write_text(child.read_text() + '\n## Edges\n\n| From | To | When |\n'
                     '| --- | --- | --- |\n| work | END | always |\n')
    (tmp_path / 'schema.json').write_text(
        '{"type":"object","properties":{"value":{"type":"string"}}}')
    entry.write_text(entry.read_text() + '\n## Bindings\n\n'
                     '| Node | Input | From | Field | Default |\n'
                     '| --- | --- | --- | --- | --- |\n'
                     '| work | value | START | value | - |\n')

    projection = project_view(admit(entry))

    assert projection.edges[-1].implicit is False
    assert projection.edges[-1].target == 'END'
    assert any(ref.kind == 'binding' and ('Input', 'value') in ref.attributes
               for ref in projection.references)
