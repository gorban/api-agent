"""Component-local declarations preserve physical ownership and legacy semantics."""
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

from api_agent.workflows.definition import InlinePromptRef, Replacement, load_definition
from api_agent.workflows.layout import LayoutError
from api_agent.workflows.provenance import verify_inline_prompts
from api_agent.workflows.stateful import load_stateful_definition
from api_agent.workflows.document_format import extract_document, validate_presentation_location, wrap_document


def node_sections(identifier='inspect', level='###'):
    return (f'{level} Native\n\n| Registry | Kind | Source |\n| --- | --- | --- |\n'
            f'| {identifier} | deterministic | [source](native.py) |\n\n'
            f'{level} Functions\n\n| ID | Registry | Input | Output |\n'
            '| --- | --- | --- | --- |\n'
            f'| {identifier} | {identifier} | [input](input.json) | [output](output.json) |\n\n'
            f'{level} Prompts\n\n| Node | File | When |\n| --- | --- | --- |\n'
            f'| {identifier} | @greeting | always |\n\n'
            f'{level} Prompt: greeting\n\n```text\nHello.\n```\n')


@pytest.fixture
def inline_graph(tmp_path):
    (tmp_path / 'native.py').write_text('def inspect(inputs, context):\n    return {"seen": True}\n')
    (tmp_path / 'input.json').write_text('{"type":"object"}')
    (tmp_path / 'output.json').write_text('{"type":"object","properties":{"seen":{"type":"boolean"}}}')
    entry = tmp_path / 'flow.md.html'
    entry.write_text('# Workflow v2\n\n## Graph ID\n\n| ID |\n| --- |\n| inline_graph |\n\n'
                     '## Flow\n\n1.  [component group](group.md.html)\n')
    component = tmp_path / 'group.md.html'
    component.write_text('# Component: group\n\n## Flow\n\n1.  [node inspect](#node-inspect)\n\n'
                         '## Components\n\n| ID | Max passes | Guard |\n| --- | --- | --- |\n| group | 1 | - |\n\n'
                         '## Node: inspect\n\n' + node_sections())
    return entry, component


def test_inline_node_has_physical_selection_fragment_and_owned_rows(inline_graph):
    entry, component = inline_graph

    definition = load_definition(entry, base_root=entry.parent, pack_root=entry.parent)

    assert definition.selected_declarations['inspect'][2] == component
    assert definition.declaration_fragments == {'inspect': 'node-inspect'}
    with pytest.raises(TypeError):
        definition.declaration_fragments['inspect'] = 'different'
    assert sum(resource.kind == 'declaration' for resource in definition.resources) == 2
    assert definition.layout.steps[1].source == entry.parent / 'native.py'
    assert definition.layout.tables['Functions'][0]['_owner_id'] == 'inspect'
    assert definition.layout.tables['Functions'][0]['_owner'] == component
    prompt = definition.layout.tables['Prompts'][0]['File']
    assert prompt == InlinePromptRef(component, 'greeting')
    assert definition.contents[prompt] == b'Hello.\n'
    verify_inline_prompts(definition)


@pytest.mark.parametrize('old,new,diagnostic', [
    ('#node-inspect', '#node-other', 'fragment'),
    ('#node-inspect', 'group.md.html#node-inspect', 'resource link'),
    ('#node-inspect', '#node-%69nspect', 'fragment'),
    ('## Node: inspect', '## Node: wrong', 'fragment'),
    ('## Node: inspect', '## Node: bad-id', 'inline node'),
    ('### Native', '#### Native', 'unknown heading'),
    ('## Node: inspect\n', '## Node: inspect\nnot blank\n', 'content before inline'),
    ('## Node: inspect', '## Node: inspect\n\n## Node: inspect', 'duplicate inline'),
])
def test_inline_declaration_rejects_ambiguous_targets_and_nesting(inline_graph, old, new, diagnostic):
    entry, component = inline_graph
    component.write_text(component.read_text().replace(old, new))

    with pytest.raises(LayoutError, match=diagnostic):
        load_definition(entry, base_root=entry.parent, pack_root=entry.parent)


def test_unreferenced_inline_node_is_rejected(inline_graph):
    entry, component = inline_graph
    component.write_text(component.read_text() + '\n## Node: unused\n\n### Native\n')

    with pytest.raises(LayoutError, match='unreferenced'):
        load_definition(entry, base_root=entry.parent, pack_root=entry.parent)


def test_fenced_fake_inline_heading_does_not_define_a_node(inline_graph):
    entry, component = inline_graph
    component.write_text(component.read_text().replace('## Node: inspect\n\n' + node_sections(),
                         '## Notes\n\n```text\n## Node: inspect\n```\n'))

    with pytest.raises(LayoutError, match='fragment'):
        load_definition(entry, base_root=entry.parent, pack_root=entry.parent)


def test_inline_original_can_be_replaced_by_a_versioned_split_node(inline_graph):
    entry, component = inline_graph
    split = entry.parent / 'replacement.md.html'
    split.write_text('# Node v2: inspect\n\n' + node_sections(level='##'))

    definition = load_definition(entry, base_root=entry.parent, pack_root=entry.parent,
        replacements=(Replacement('inspect', Path(split.name), 'base'),))

    assert definition.selected_declarations['inspect'][2] == split
    assert not definition.declaration_fragments
    assert component in definition.contents


def test_prompt_provenance_rejects_changed_scoped_body(inline_graph):
    entry, component = inline_graph
    definition = load_definition(entry, base_root=entry.parent, pack_root=entry.parent)
    contents = dict(definition.contents)
    contents[component] = contents[component].replace(b'Hello.', b'Changed.')

    with pytest.raises(LayoutError, match='differs from its admitted parent'):
        verify_inline_prompts(replace(definition, contents=MappingProxyType(contents)))


def test_inline_node_cannot_consume_component_prompt(inline_graph):
    entry, component = inline_graph
    component.write_text(component.read_text().replace(
        '## Node: inspect',
        '## Prompt: foreign\n\n```text\nWrong scope.\n```\n\n## Node: inspect').replace(
        '| inspect | @greeting | always |', '| inspect | @foreign | always |'))

    with pytest.raises(LayoutError, match='foreign inline prompt scope'):
        load_definition(entry, base_root=entry.parent, pack_root=entry.parent)


def test_component_prompt_row_can_target_its_inline_descendant(inline_graph):
    entry, component = inline_graph
    component.write_text(component.read_text().replace(
        '## Node: inspect',
        '## Prompts\n\n| Node | File | When |\n| --- | --- | --- |\n'
        '| inspect | @shared | always |\n\n'
        '## Prompt: shared\n\n```text\nShared.\n```\n\n## Node: inspect'))

    definition = load_definition(entry, base_root=entry.parent, pack_root=entry.parent)

    row = next(row for row in definition.layout.tables['Prompts'] if row['_owner_id'] == 'group')
    assert row['File'] == InlinePromptRef(component, 'shared')
    assert definition.contents[row['File']] == b'Shared.\n'
    verify_inline_prompts(definition)


def test_component_prompt_row_cannot_target_an_unowned_node(inline_graph):
    entry, component = inline_graph
    outside = entry.parent / 'outside.md.html'
    outside.write_text('# Node: outside\n\n## Native\n\n| Registry | Kind | Source |\n'
                       '| --- | --- | --- |\n| outside | deterministic | [source](native.py) |\n\n'
                       '## Functions\n\n| ID | Registry | Input | Output |\n'
                       '| --- | --- | --- | --- |\n'
                       '| outside | outside | [input](input.json) | [output](output.json) |\n')
    entry.write_text(entry.read_text() + '2.  [node outside](outside.md.html)\n')
    component.write_text(component.read_text().replace(
        '## Node: inspect',
        '## Prompts\n\n| Node | File | When |\n| --- | --- | --- |\n'
        '| outside | @shared | always |\n\n'
        '## Prompt: shared\n\n```text\nShared.\n```\n\n## Node: inspect'))

    with pytest.raises(LayoutError, match='foreign inline prompt scope'):
        load_definition(entry, base_root=entry.parent, pack_root=entry.parent)


def test_root_prompt_row_can_use_its_target_nodes_legacy_prompt(inline_graph):
    entry, component = inline_graph
    entry.write_text(entry.read_text() + '\n## Prompts\n\n| Node | File | When |\n'
                     '| --- | --- | --- |\n| inspect | @greeting | always |\n')
    component.write_text(component.read_text().replace(
        '### Prompts\n\n| Node | File | When |\n| --- | --- | --- |\n'
        '| inspect | @greeting | always |\n\n', ''))

    definition = load_definition(entry, base_root=entry.parent, pack_root=entry.parent)

    assert definition.layout.tables['Prompts'][0]['File'] == InlinePromptRef(component, 'greeting')
    verify_inline_prompts(definition)


def test_replacement_cannot_borrow_prompt_from_discarded_inline_node(inline_graph):
    entry, _ = inline_graph
    split = entry.parent / 'replacement.md.html'
    split.write_text('# Node: inspect\n\n' + node_sections(level='##').replace(
        '## Prompt: greeting\n\n```text\nHello.\n```\n', ''))

    with pytest.raises(LayoutError, match='unknown inline prompt'):
        load_definition(entry, base_root=entry.parent, pack_root=entry.parent,
                        replacements=(Replacement('inspect', Path(split.name), 'base'),))


def test_provenance_rejects_discarded_inline_prompt_even_when_parent_bytes_match(inline_graph):
    entry, component = inline_graph
    split = entry.parent / 'replacement.md.html'
    split.write_text('# Node: inspect\n\n' + node_sections(level='##'))
    definition = load_definition(entry, base_root=entry.parent, pack_root=entry.parent,
                                 replacements=(Replacement('inspect', Path(split.name), 'base'),))
    forged = InlinePromptRef(component, 'greeting')
    contents = dict(definition.contents)
    contents[forged] = b'Hello.\n'

    with pytest.raises(LayoutError, match='selected declaration scope'):
        verify_inline_prompts(replace(definition, contents=MappingProxyType(contents)))


def test_provenance_rejects_prompt_whose_selected_scope_points_to_another_file(inline_graph):
    entry, _ = inline_graph
    definition = load_definition(entry, base_root=entry.parent, pack_root=entry.parent)
    selected = dict(definition.selected_declarations)
    owner, line, _ = selected['inspect']
    selected['inspect'] = owner, line, entry

    with pytest.raises(LayoutError, match='selected declaration scope'):
        verify_inline_prompts(replace(definition,
                                      selected_declarations=MappingProxyType(selected)))


def test_new_root_editor_comment_precedes_metadata_and_preserves_old_envelope(inline_graph):
    from api_agent.workflows import document_format as format

    entry, _ = inline_graph
    executable = entry.read_bytes()
    wrapped = wrap_document(executable, root=True, asset_prefix='.private_resources/viewer/')
    assert wrapped.startswith(format._HINT + format._ROOT_INSTRUCTIONS)
    assert wrapped.index(b'Open this file') < wrapped.index(b'<meta charset')
    assert extract_document(wrapped, path=entry).executable_bytes == executable
    validate_presentation_location(wrapped, path=entry, root=entry.parent)
    old = format._HEAD.replace(b'{assets}', b'.private_resources/viewer/') + format._ROOT_FALLBACK + format._CARRIER + executable
    validate_presentation_location(old, path=entry, root=entry.parent)
    assert extract_document(old, path=entry).executable_bytes == executable


def test_presented_inline_rows_keep_exact_physical_lines(inline_graph):
    entry, component = inline_graph
    component.write_bytes(wrap_document(component.read_bytes(), root=False,
                                       asset_prefix='.private_resources/viewer/'))

    definition = load_definition(entry, base_root=entry.parent, pack_root=entry.parent)

    line = next(index for index, text in enumerate(component.read_text().splitlines(), 1)
                if text.startswith('| inspect | inspect |'))
    assert definition.layout.tables['Functions'][0]['line'] == line


def local_diagram(target=b'#node-inspect', label=b'inspect'):
    svg = b'.private_resources/diagrams/group.svg'
    return (b'![Workflow diagram](' + svg + b')\n\n'
            b'[Open standalone diagram](' + svg + b')\n\n'
            b'- [' + label + b'](' + target + b')')


def test_saved_component_diagram_admits_its_declared_local_node(inline_graph):
    _, component = inline_graph
    executable = component.read_bytes()
    saved = wrap_document(executable, root=False,
                          asset_prefix='.private_resources/viewer/',
                          generated=local_diagram())

    extracted = extract_document(saved, path=component)

    assert extracted.executable_bytes == executable
    assert extracted.generated == local_diagram()
    validate_presentation_location(saved, path=component, root=component.parent)


@pytest.mark.parametrize('target,label', [
    (b'#node-other', b'inspect'),
    (b'#node-inspect', b'other'),
    (b'#other-inspect', b'inspect'),
    (b'#node-%69nspect', b'inspect'),
    (b'#node-inspect\x00', b'inspect'),
    (b'javascript:alert(1)', b'inspect'),
])
def test_saved_component_diagram_rejects_foreign_or_unsafe_local_target(inline_graph, target, label):
    _, component = inline_graph
    saved = wrap_document(component.read_bytes(), root=False,
                          asset_prefix='.private_resources/viewer/',
                          generated=local_diagram(target, label))

    with pytest.raises(LayoutError, match='generated'):
        validate_presentation_location(saved, path=component, root=component.parent)


def test_fenced_fake_node_heading_cannot_authorize_generated_fragment(inline_graph):
    _, component = inline_graph
    component.write_text(component.read_text().replace(
        '## Node: inspect\n\n' + node_sections(),
        '## Notes\n\n```text\n## Node: inspect\n```\n'))
    saved = wrap_document(component.read_bytes(), root=False,
                          asset_prefix='.private_resources/viewer/',
                          generated=local_diagram())

    with pytest.raises(LayoutError, match='no local declaration'):
        validate_presentation_location(saved, path=component, root=component.parent)


def stateful_graph(tmp_path, version='alias'):
    (tmp_path / 'native.py').write_text('from typing import TypedDict\nclass State(TypedDict):\n    seen: bool\n'
                                       'def inspect(state, context):\n    return {"seen": True}\n')
    entry = tmp_path / 'flow.md.html'
    entry.write_text('# Workflow v3\n\n## Graph ID\n\n| ID |\n| --- |\n| stateful_inline |\n\n'
                     '## State\n\n| Registry | Source |\n| --- | --- |\n| State | [Python](native.py) |\n\n'
                     '## Flow\n\n1.  [component group](group.md.html)\n\n'
                     '## Edges\n\n| From | To | Router | Source |\n| --- | --- | --- | --- |\n'
                     '| group | END | - | - |\n| inspect | END | - | - |\n')
    component = tmp_path / 'group.md.html'
    heading = '# Component v3: group' if version == 'v3' else '# Component: group'
    component.write_text(heading + '\n\n## Flow\n\n1.  [node inspect](#node-inspect)\n\n'
                         '## Node: inspect\n\n### Native\n\n| Registry | Kind | Source |\n'
                         '| --- | --- | --- |\n| inspect | deterministic | [Python](native.py) |\n')
    return entry, component


@pytest.mark.parametrize('version', ['v3', 'alias'])
def test_stateful_component_inline_leaf_keeps_native_semantics_and_fragments(tmp_path, version):
    entry, component = stateful_graph(tmp_path, version)

    definition = load_stateful_definition(entry, base_root=tmp_path, pack_root=tmp_path)

    assert definition.declaration_fragments == {'inspect': 'node-inspect'}
    assert definition.selected_declarations['inspect'][2] == component
    assert definition.layout.steps[1].source == tmp_path / 'native.py'
    assert definition.nodes[0]['_owner_id'] == 'inspect'
    assert len([resource for resource in definition.resources if resource.kind == 'declaration']) == 2


def test_stateful_replacement_keeps_selected_split_node(tmp_path):
    entry, component = stateful_graph(tmp_path)
    split = tmp_path / 'replacement.md.html'
    split.write_text('# Node v3: inspect\n\n## Native\n\n| Registry | Kind | Source |\n'
                     '| --- | --- | --- |\n| inspect | deterministic | [Python](native.py) |\n')

    definition = load_stateful_definition(entry, base_root=tmp_path, pack_root=tmp_path,
        replacements=(Replacement('inspect', Path(split.name), 'base'),))

    assert definition.selected_declarations['inspect'][2] == split
    assert not definition.declaration_fragments
    assert component in definition.contents
    assert definition.nodes[0]['Source'] == tmp_path / 'native.py'


def test_stateful_rejects_unreferenced_inline_node(tmp_path):
    entry, component = stateful_graph(tmp_path)
    component.write_text(component.read_text() + '\n## Node: spare\n\n### Native\n')

    with pytest.raises(LayoutError, match='unreferenced inline node declaration'):
        load_stateful_definition(entry, base_root=tmp_path, pack_root=tmp_path)


@pytest.mark.parametrize('old,new,diagnostic', [
    ('#node-inspect', '#node-other', 'fragment'),
    ('## Node: inspect', '## Node: inspect\n\n## Node: inspect', 'duplicate inline'),
    ('### Native', '#### Native', 'unknown heading'),
])
def test_stateful_inline_leaf_rejects_foreign_duplicate_or_nested_structure(tmp_path, old, new, diagnostic):
    entry, component = stateful_graph(tmp_path)
    component.write_text(component.read_text().replace(old, new))

    with pytest.raises(LayoutError, match=diagnostic):
        load_stateful_definition(entry, base_root=tmp_path, pack_root=tmp_path)


@pytest.mark.parametrize('version', ['v2', 'alias'])
def test_optional_split_node_heading_forms_remain_compatible(inline_graph, version):
    entry, component = inline_graph
    split = entry.parent / 'split.md.html'
    heading = '# Node v2: inspect' if version == 'v2' else '# Node: inspect'
    split.write_text(heading + '\n\n' + node_sections(level='##'))
    component.write_text(component.read_text().partition('## Node: inspect')[0].replace(
        '#node-inspect', split.name))

    definition = load_definition(entry, base_root=entry.parent, pack_root=entry.parent)

    assert not definition.declaration_fragments
    assert definition.selected_declarations['inspect'][2] == split
