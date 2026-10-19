"""Versioned declarations admit one stable, complete graph snapshot."""

from pathlib import Path
import asyncio

import pytest

from api_agent.workflows.definition import Replacement, load_definition, load_native_catalog
from api_agent.workflows.definition import InlinePromptRef, native_origin, _sections, _table
from api_agent.workflows.execution import NodeFunction, Workflow
from api_agent.workflows.layout import LayoutError


def make_graph(root: Path):
    (root / 'workflows').mkdir()
    (root / 'schemas').mkdir()
    (root / 'native.py').write_text('def inspect(inputs, context):\n    return {"seen": True}\n')
    (root / 'schemas' / 'input.json').write_text('{"type":"object"}')
    (root / 'schemas' / 'output.json').write_text('{"type":"object","properties":{"seen":{"type":"boolean"}}}')
    (root / 'flow.md.html').write_text(
        '# Workflow v2\n\n## Graph ID\n\n| ID |\n| --- |\n| sample_graph |\n\n'
        '## Flow\n\n1.  [node inspect](workflows/inspect.md.html)\n\n'
        '## Bindings\n\n| Node | Input | From | Field | Default |\n'
        '| --- | --- | --- | --- | --- |\n')
    (root / 'workflows' / 'inspect.md.html').write_text(
        '# Node v2: inspect\n\n## Native\n\n| Registry | Kind | Source |\n'
        '| --- | --- | --- |\n| inspect | deterministic | [source](../native.py) |\n\n'
        '## Functions\n\n| ID | Registry | Input | Output |\n'
        '| --- | --- | --- | --- |\n'
        '| inspect | inspect | [input](../schemas/input.json) | [output](../schemas/output.json) |\n\n'
        '## Prompt: greeting\n\n```text\nHello.\n```\n\n'
        '## Prompts\n\n| Node | File | When |\n| --- | --- | --- |\n'
        '| inspect | @greeting | always |\n')
    return root / 'flow.md.html'


def test_load_definition_captures_linked_and_inline_resources(tmp_path):
    entry = make_graph(tmp_path)

    definition = load_definition(entry, base_root=tmp_path, pack_root=tmp_path)

    assert definition.graph_id == 'sample_graph'
    assert definition.layout.steps[0].source == tmp_path / 'native.py'
    assert definition.contents[definition.layout.tables['Prompts'][0]['File']] == b'Hello.\n'
    assert {resource.kind for resource in definition.resources} >= {'declaration', 'schema', 'native_source', 'prompt'}


def test_definition_identity_ignores_absolute_install_path(tmp_path):
    first = tmp_path / 'one'
    second = tmp_path / 'two'
    first.mkdir()
    second.mkdir()
    first_id = load_definition(make_graph(first), base_root=first, pack_root=first).definition_id
    second_id = load_definition(make_graph(second), base_root=second, pack_root=second).definition_id
    assert first_id == second_id


def test_replacement_requires_known_target_and_declared_root(tmp_path):
    entry = make_graph(tmp_path)
    with pytest.raises(LayoutError, match='unknown replacement'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path,
                        replacements=(Replacement('missing', Path('workflows/inspect.md.html'), 'pack'),))


def test_html_is_rejected_before_execution(tmp_path):
    entry = make_graph(tmp_path)
    entry.write_text(entry.read_text() + '\n<script>alert(1)</script>\n')
    with pytest.raises(LayoutError, match='HTML'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path)


def test_component_owns_nested_flow_without_root_repetition(tmp_path):
    entry = make_graph(tmp_path)
    (tmp_path / 'workflows' / 'review.md.html').write_text(
        '# Component v2: review\n\n## Flow\n\n'
        '1.  [node inspect](inspect.md.html)\n\n'
        '## Components\n\n| ID | Max passes | Guard |\n'
        '| --- | --- | --- |\n| review | 2 | inspect |\n')
    entry.write_text(entry.read_text().replace(
        '1.  [node inspect](workflows/inspect.md.html)',
        '1.  [component review](workflows/review.md.html)'))

    definition = load_definition(entry, base_root=tmp_path, pack_root=tmp_path)

    assert [(step.id, step.parent) for step in definition.layout.steps] == [
        ('review', None), ('inspect', 'review')]


def test_native_source_change_after_admission_is_rejected(tmp_path):
    definition = load_definition(make_graph(tmp_path), base_root=tmp_path, pack_root=tmp_path)
    (tmp_path / 'native.py').write_text('def inspect(inputs, context):\n    return {"seen": False}\n')

    with pytest.raises(LayoutError, match='changed after definition admission'):
        definition.verify_native_sources()


def test_execution_uses_admitted_prompt_bytes(tmp_path):
    entry = make_graph(tmp_path)
    (tmp_path / 'native.py').write_text(
        'def inspect(inputs, context):\n'
        '    return {"seen": context.prompts == ("Hello.\\n",)}\n')
    definition = load_definition(entry, base_root=tmp_path, pack_root=tmp_path)
    namespace = load_native_catalog(definition, tmp_path / 'native.py')
    workflow = Workflow(definition, {'inspect': NodeFunction(namespace['inspect'])})
    (tmp_path / 'workflows' / 'inspect.md.html').write_text('corrupted after admission')

    result = asyncio.run(workflow.arun({}))

    assert result['inspect'] == {'seen': True}


def test_inline_prompt_preserves_blank_lines_and_heading_like_text(tmp_path):
    entry = make_graph(tmp_path)
    node = tmp_path / 'workflows' / 'inspect.md.html'
    node.write_text(node.read_text().replace('Hello.\n```', 'Hello.\n\n# literal heading\n<tag>\n```'))

    definition = load_definition(entry, base_root=tmp_path, pack_root=tmp_path)

    prompt = definition.layout.tables['Prompts'][0]['File']
    assert definition.contents[prompt] == b'Hello.\n\n# literal heading\n<tag>\n'


def test_known_replacement_changes_definition_identity(tmp_path):
    entry = make_graph(tmp_path)
    alternate = tmp_path / 'workflows' / 'alternate.md.html'
    alternate.write_text((tmp_path / 'workflows' / 'inspect.md.html').read_text())

    base = load_definition(entry, base_root=tmp_path, pack_root=tmp_path)
    replaced = load_definition(entry, base_root=tmp_path, pack_root=tmp_path,
                               replacements=(Replacement('inspect', Path('workflows/alternate.md.html'), 'pack'),))

    assert replaced.layout.tables['Functions'][0]['ID'] == 'inspect'
    assert replaced.definition_id != base.definition_id


def test_replacement_captures_only_selected_declaration_closure(tmp_path):
    entry = make_graph(tmp_path)
    original = tmp_path / 'workflows' / 'inspect.md.html'
    alternate = tmp_path / 'workflows' / 'alternate.md.html'
    alternate.write_bytes(original.read_bytes())
    selected = (Replacement('inspect', Path('workflows/alternate.md.html'), 'pack'),)
    admitted = load_definition(entry, base_root=tmp_path, pack_root=tmp_path,
                               replacements=selected)
    original.write_text('edited unselected declaration')
    after_edit = load_definition(entry, base_root=tmp_path, pack_root=tmp_path,
                                 replacements=selected)
    original.unlink()
    after_delete = load_definition(entry, base_root=tmp_path, pack_root=tmp_path,
                                   replacements=selected)
    outside = tmp_path.parent / 'unselected-target.txt'
    outside.write_text('unselected external data')
    original.symlink_to(outside)
    after_symlink = load_definition(entry, base_root=tmp_path, pack_root=tmp_path,
                                    replacements=selected)

    assert admitted.definition_id == after_edit.definition_id == after_delete.definition_id == after_symlink.definition_id
    assert all(resource.logical_path != 'base/workflows/inspect.md.html'
               for resource in after_delete.resources)
    assert after_delete.layout.steps[0].source == tmp_path / 'native.py'


def test_replacement_still_checks_original_link_syntax_and_kind(tmp_path):
    entry = make_graph(tmp_path)
    alternate = tmp_path / 'workflows' / 'alternate.md.html'
    alternate.write_bytes((tmp_path / 'workflows' / 'inspect.md.html').read_bytes())
    selected = (Replacement('inspect', Path('workflows/alternate.md.html'), 'pack'),)
    entry.write_text(entry.read_text().replace('(workflows/inspect.md.html)',
                                               '(https://example.invalid/inspect.md.html)'))
    with pytest.raises(LayoutError, match='invalid resource link'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path,
                        replacements=selected)
    entry.write_text(entry.read_text().replace('[node inspect]', '[component inspect]').replace(
        '(https://example.invalid/inspect.md.html)', '(workflows/inspect.md.html)'))
    with pytest.raises(LayoutError, match='declaration identity mismatch'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path,
                        replacements=selected)
    entry.write_text(entry.read_text().replace('[component inspect]', '[node inspect]').replace(
        '(workflows/inspect.md.html)', '(../../outside.md.html)'))
    with pytest.raises(LayoutError, match='outside declared roots'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path,
                        replacements=selected)


def test_native_loader_rejects_stale_source_before_top_level_code(tmp_path, monkeypatch):
    entry = make_graph(tmp_path)
    source = tmp_path / 'native.py'
    source.write_text('raise RuntimeError("top-level code ran")\n'
                      'def inspect(inputs, context):\n    return {"seen": True}\n')
    definition = load_definition(entry, base_root=tmp_path, pack_root=tmp_path)
    source.write_text('def inspect(inputs, context):\n    return {"seen": False}\n')

    with pytest.raises(LayoutError, match='changed after definition admission'):
        load_native_catalog(definition, source)
    # Focused mutation control: removing only freshness admission reaches top-level code.
    with monkeypatch.context() as mutation:
        mutation.setattr(type(definition), 'verify_native_sources', lambda self: None)
        with pytest.raises(RuntimeError, match='top-level code ran'):
            load_native_catalog(definition, source)


def test_definition_rejects_resource_change_during_admission(tmp_path, monkeypatch):
    import api_agent.workflows.definition as module
    entry = make_graph(tmp_path)
    source = tmp_path / 'native.py'
    original = module._read_bounded
    count = 0

    def changing_read(path, root):
        nonlocal count
        if path == source:
            count += 1
            if count == 2:
                return b'changed during admission'
        return original(path, root)

    monkeypatch.setattr(module, '_read_bounded', changing_read)

    with pytest.raises(LayoutError, match='changed during definition admission'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path)


def test_definition_exposes_immutable_plan_rows_and_contents(tmp_path):
    definition = load_definition(make_graph(tmp_path), base_root=tmp_path, pack_root=tmp_path)

    with pytest.raises(TypeError):
        definition.layout.tables['Functions'][0]['Registry'] = 'different'
    with pytest.raises(TypeError):
        definition.contents[definition.entry] = b'changed'


def test_stale_callable_from_same_path_is_not_admitted(tmp_path):
    entry = make_graph(tmp_path)
    old_definition = load_definition(entry, base_root=tmp_path, pack_root=tmp_path)
    old = load_native_catalog(old_definition, tmp_path / 'native.py')['inspect']
    (tmp_path / 'native.py').write_text('def inspect(inputs, context):\n    return {"seen": False}\n')
    current = load_definition(entry, base_root=tmp_path, pack_root=tmp_path)

    with pytest.raises(LayoutError, match='does not belong to the admitted definition'):
        Workflow(current, {'inspect': NodeFunction(old)})
    fresh = load_native_catalog(current, tmp_path / 'native.py')['inspect']
    assert Workflow(current, {'inspect': NodeFunction(fresh)})


def test_inline_prompt_cannot_alias_real_linked_file(tmp_path):
    entry = make_graph(tmp_path)
    external = tmp_path / '.inline' / 'greeting.txt'
    external.parent.mkdir()
    external.write_text('External prompt\n')
    entry.write_text(entry.read_text() + '\n## Prompts\n\n| Node | File | When |\n'
                     '| --- | --- | --- |\n| inspect | [external](.inline/greeting.txt) | always |\n')

    definition = load_definition(entry, base_root=tmp_path, pack_root=tmp_path)

    assert {definition.contents[row['File']] for row in definition.layout.tables['Prompts']} == {
        b'Hello.\n', b'External prompt\n'}
    assert any(item.logical_path == 'base/.inline/greeting.txt' for item in definition.resources)


def test_percent_encoded_local_filename_resolves_once(tmp_path):
    entry = make_graph(tmp_path)
    node = tmp_path / 'workflows' / 'inspect.md.html'
    spaced = tmp_path / 'workflows' / 'inspect with space.md.html'
    node.rename(spaced)
    entry.write_text(entry.read_text().replace('inspect.md.html', 'inspect%20with%20space.md.html'))

    definition = load_definition(entry, base_root=tmp_path, pack_root=tmp_path)

    assert definition.layout.tables['Functions'][0]['ID'] == 'inspect'


def test_component_depth_rejected_with_located_diagnostic(tmp_path):
    entry = make_graph(tmp_path)
    for index in range(66):
        name = f'component_{index}'
        target = f'component_{index + 1}' if index < 65 else 'inspect'
        kind = 'component' if index < 65 else 'node'
        (tmp_path / 'workflows' / f'{name}.md.html').write_text(
            f'# Component v2: {name}\n\n## Flow\n\n'
            f'1.  [{kind} {target}]({target}.md.html)\n')
    entry.write_text(entry.read_text().replace('node inspect](workflows/inspect.md.html)',
                                               'component component_0](workflows/component_0.md.html)'))

    with pytest.raises(LayoutError, match='nesting exceeds 64'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path)


def test_pack_identity_stable_when_pack_moves_inside_base(tmp_path):
    nested_base = tmp_path / 'nested_base'
    sibling_base = tmp_path / 'sibling_base'
    nested_pack = nested_base / 'pack'
    sibling_pack = tmp_path / 'pack'
    for path in (nested_base, sibling_base, nested_pack, sibling_pack):
        path.mkdir(exist_ok=True)
    nested_entry = make_graph(nested_pack)
    sibling_entry = make_graph(sibling_pack)

    nested = load_definition(nested_entry, base_root=nested_base, pack_root=nested_pack)
    sibling = load_definition(sibling_entry, base_root=sibling_base, pack_root=sibling_pack)

    assert nested.definition_id == sibling.definition_id
    assert all(item.logical_path.startswith(('pack/', 'inline/pack/')) for item in nested.resources)


@pytest.mark.parametrize(('old', 'new', 'message'), [
    ('# Workflow v2', '# Workflow v3', 'Workflow v2'),
    ('## Graph ID', '## Missing', 'unknown or missing'),
    ('| sample_graph |', '| Invalid-ID |', 'stable ID'),
    ('1.  [node inspect]', '2.  [node inspect]', 'step sequence'),
    ('1.  [node inspect]', '1. [node inspect]', 'step sequence'),
    ('1.  [node inspect]', '1.  [tool inspect]', 'step label'),
    ('1.  [node inspect]', '    1.  [node inspect]', 'direct ordered'),
    ('(workflows/inspect.md.html)', '(workflows/inspect.txt)', 'end .md.html'),
    ('(workflows/inspect.md.html)', '(https://example.invalid/x)', 'invalid resource link'),
    ('(workflows/inspect.md.html)', '(workflows/%2finspect.md.html)', 'invalid resource link'),
    ('(workflows/inspect.md.html)', '(workflows/missing.md.html)', 'unavailable resource'),
])
def test_root_admission_rejects_incompatible_structure(tmp_path, old, new, message):
    entry = make_graph(tmp_path)
    if new == '(workflows/inspect.txt)':
        (tmp_path / 'workflows' / 'inspect.txt').write_text('# Node v2: inspect\n')
    entry.write_text(entry.read_text().replace(old, new, 1))

    with pytest.raises(LayoutError, match=message):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path)


@pytest.mark.parametrize(('old', 'new', 'message'), [
    ('# Node v2: inspect', '# Node v2: other', 'identity mismatch'),
    ('## Native', '## Tool', 'unknown declaration section'),
    ('| inspect | deterministic |', '| inspect | tool |', 'unsupported native callable kind'),
    ('| inspect | deterministic |', '| other | deterministic |', 'Native registry'),
    ('## Functions', '## Unknown', 'unknown declaration section'),
    ('| inspect | inspect | [input]', '| other | inspect | [input]', 'Native registry'),
    ('@greeting', '@missing', 'unknown inline prompt'),
    ('```text', '```python', 'invalid inline prompt'),
])
def test_child_admission_rejects_incompatible_binding(tmp_path, old, new, message):
    entry = make_graph(tmp_path)
    node = tmp_path / 'workflows' / 'inspect.md.html'
    node.write_text(node.read_text().replace(old, new, 1))

    with pytest.raises(LayoutError, match=message):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path)


def test_admission_rejects_duplicate_step_and_replacement(tmp_path):
    entry = make_graph(tmp_path)
    entry.write_text(entry.read_text().replace('## Bindings',
                     '2.  [node inspect](workflows/inspect.md.html)\n\n## Bindings'))
    with pytest.raises(LayoutError, match='duplicate step ID'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path)
    entry.write_text(entry.read_text().replace('2.  [node inspect](workflows/inspect.md.html)\n\n', ''))
    replacement = Replacement('inspect', Path('workflows/inspect.md.html'), 'pack')
    with pytest.raises(LayoutError, match='duplicate replacement'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path,
                        replacements=(replacement, replacement))


def test_admission_rejects_undeclared_entry_and_native_catalog(tmp_path):
    entry = make_graph(tmp_path)
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    with pytest.raises(LayoutError, match='invalid declared entry root'):
        load_definition(entry, base_root=tmp_path, pack_root=elsewhere, entry_root='pack')
    definition = load_definition(entry, base_root=tmp_path, pack_root=tmp_path)
    with pytest.raises(LayoutError, match='not a selected native binding'):
        load_native_catalog(definition, entry)


def test_inline_prompt_reference_links_to_owning_declaration(tmp_path):
    definition = load_definition(make_graph(tmp_path), base_root=tmp_path, pack_root=tmp_path)
    prompt = definition.layout.tables['Prompts'][0]['File']
    assert isinstance(prompt, InlinePromptRef)
    assert prompt.as_uri().endswith('/workflows/inspect.md.html#prompt-greeting')
    assert native_origin([]) is None


def test_native_source_verification_handles_component_and_unavailable_file(tmp_path):
    entry = make_graph(tmp_path)
    component = tmp_path / 'workflows' / 'review.md.html'
    component.write_text('# Component v2: review\n\n## Flow\n\n'
                         '1.  [node inspect](inspect.md.html)\n\n'
                         '## Components\n\n| ID | Max passes | Guard |\n'
                         '| --- | --- | --- |\n| review | 1 | inspect |\n')
    entry.write_text(entry.read_text().replace('node inspect](workflows/inspect.md.html)',
                                               'component review](workflows/review.md.html)'))
    definition = load_definition(entry, base_root=tmp_path, pack_root=tmp_path)
    definition.verify_native_sources()
    (tmp_path / 'native.py').unlink()
    with pytest.raises(LayoutError, match='unavailable resource'):
        definition.verify_native_sources()


@pytest.mark.parametrize(('data', 'message'), [
    (b'\xff', 'UTF-8'),
    (b'', 'empty declaration'),
    (b'not a heading\n', 'version heading'),
    (b'# Workflow v2\n\nstray prose\n\n## Flow\n', 'content before section'),
    (b'# Workflow v2\n\n### Flow\n', 'unknown heading'),
    (b'# Workflow v2\n\n## Flow\n\n## Flow\n', 'duplicate section'),
    (b'# *Workflow* v2\n', 'heading must be plain text'),
])
def test_markdown_structure_diagnostics(tmp_path, data, message):
    with pytest.raises(LayoutError, match=message):
        _sections(tmp_path / 'flow.md.html', data)


@pytest.mark.parametrize(('lines', 'message'), [
    (['| A |'], 'incomplete table'),
    (['not a table', 'still not'], 'one Markdown table'),
    (['A |', '| --- |', '| x |'], 'fixed-column table'),
    (['| A |', '| --- |', '| x | y |'], 'column count'),
    (['| B |', '| --- |', '| x |'], 'unknown table columns'),
    (['| A |', '| nope |', '| x |'], 'one Markdown table'),
])
def test_fixed_table_rejects_ambiguous_shape(tmp_path, lines, message):
    with pytest.raises(LayoutError, match=message):
        _table(tmp_path / 'flow.md.html', list(enumerate(lines, 1)), ('A',))


def test_unknown_entry_and_replacement_roots_rejected(tmp_path):
    entry = make_graph(tmp_path)
    other = tmp_path / 'other'
    other.mkdir()
    with pytest.raises(LayoutError, match='outside declared roots'):
        load_definition(entry, base_root=other, pack_root=other)
    with pytest.raises(LayoutError, match='invalid or duplicate replacement'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path,
                        replacements=(Replacement('inspect', Path('workflows/inspect.md.html'), 'unknown'),))


def test_replacement_cannot_escape_declared_root(tmp_path):
    entry = make_graph(tmp_path)
    with pytest.raises(LayoutError, match='root-relative'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path,
                        replacements=(Replacement('inspect', Path('../outside.md.html'), 'pack'),))


def test_replacement_declaration_requires_md_html_suffix(tmp_path):
    entry = make_graph(tmp_path)
    original = tmp_path / 'workflows' / 'inspect.md.html'
    alternate = tmp_path / 'workflows' / 'alternate.txt'
    alternate.write_bytes(original.read_bytes())
    with pytest.raises(LayoutError, match='replacement declaration must end .md.html'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path,
                        replacements=(Replacement('inspect', Path('workflows/alternate.txt'), 'pack'),))
    alternate.rename(tmp_path / 'workflows' / 'alternate.md.html')
    selected = load_definition(entry, base_root=tmp_path, pack_root=tmp_path,
                               replacements=(Replacement('inspect', Path('workflows/alternate.md.html'), 'pack'),))
    assert selected.layout.tables['Functions'][0]['ID'] == 'inspect'


def test_replacement_symlink_target_must_be_md_html(tmp_path):
    entry = make_graph(tmp_path)
    original = tmp_path / 'workflows' / 'inspect.md.html'
    alternate = tmp_path / 'workflows' / 'alternate.txt'
    alternate.write_bytes(original.read_bytes())
    (tmp_path / 'workflows' / 'selected.md.html').symlink_to(alternate)
    with pytest.raises(LayoutError, match='replacement declaration must end .md.html'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path,
                        replacements=(Replacement('inspect', Path('workflows/selected.md.html'), 'pack'),))


def test_encoded_separator_and_invalid_utf8_link_rejected(tmp_path):
    entry = make_graph(tmp_path)
    for destination in ('workflows/%5cinspect.md.html', 'workflows/%FF.md.html'):
        entry.write_text(entry.read_text().replace('workflows/inspect.md.html', destination))
        with pytest.raises(LayoutError, match='invalid'):
            load_definition(entry, base_root=tmp_path, pack_root=tmp_path)
        entry.write_text(entry.read_text().replace(destination, 'workflows/inspect.md.html'))


@pytest.mark.parametrize(('old', 'new', 'message'), [
    ('[source](../native.py)', 'native.py', 'inline file link'),
    ('[source](../native.py)', '[source](../native.py%3Fbad)', 'invalid resource link'),
    ('## Native\n\n| Registry | Kind | Source |\n| --- | --- | --- |\n'
     '| inspect | deterministic | [source](../native.py) |',
     '', 'one Native binding'),
    ('## Prompt: greeting', '## Prompt: bad-id', 'invalid inline prompt'),
])
def test_node_rejects_broken_resource_or_prompt(tmp_path, old, new, message):
    entry = make_graph(tmp_path)
    node = tmp_path / 'workflows' / 'inspect.md.html'
    node.write_text(node.read_text().replace(old, new, 1))
    with pytest.raises(LayoutError, match=message):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path)


def test_component_and_node_ownership_sections_are_required(tmp_path):
    entry = make_graph(tmp_path)
    component = tmp_path / 'workflows' / 'review.md.html'
    component.write_text('# Component v2: review\n')
    entry.write_text(entry.read_text().replace('node inspect](workflows/inspect.md.html)',
                                               'component review](workflows/review.md.html)'))
    with pytest.raises(LayoutError, match='component requires Flow'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path)
    component.write_text('# Component v2: review\n\n## Flow\n\n'
                         '1.  [node inspect](inspect.md.html)\n\n'
                         '## Native\n\n| Registry | Kind | Source |\n'
                         '| --- | --- | --- |\n| inspect | deterministic | [source](../native.py) |\n')
    with pytest.raises(LayoutError, match='component cannot bind native'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path)
    entry.write_text(entry.read_text().replace('component review](workflows/review.md.html)',
                                               'node inspect](workflows/inspect.md.html)'))
    node = tmp_path / 'workflows' / 'inspect.md.html'
    node.write_text(node.read_text() + '\n## Flow\n\n1.  [node child](child.md.html)\n')
    with pytest.raises(LayoutError, match='node cannot own Flow'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path)


def test_empty_component_flow_and_wrong_child_identity_rejected(tmp_path):
    entry = make_graph(tmp_path)
    component = tmp_path / 'workflows' / 'review.md.html'
    component.write_text('# Component v2: review\n\n## Flow\n')
    entry.write_text(entry.read_text().replace('node inspect](workflows/inspect.md.html)',
                                               'component review](workflows/review.md.html)'))
    with pytest.raises(LayoutError, match='Flow requires steps'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path)
    component.write_text('# Component v2: other\n\n## Flow\n\n'
                         '1.  [node inspect](inspect.md.html)\n')
    with pytest.raises(LayoutError, match='identity mismatch'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path)


def test_admission_rejects_unavailable_resource_on_final_read(tmp_path, monkeypatch):
    import api_agent.workflows.definition as module
    entry = make_graph(tmp_path)
    source = tmp_path / 'native.py'
    original = module._read_bounded
    reads = 0
    def vanishing(path, root):
        nonlocal reads
        if path == source:
            reads += 1
            if reads == 2:
                source.unlink()
        return original(path, root)
    monkeypatch.setattr(module, '_read_bounded', vanishing)
    with pytest.raises(LayoutError, match='changed during definition admission'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path)


def test_nonweakrefable_catalog_value_does_not_break_admission(tmp_path):
    entry = make_graph(tmp_path)
    source = tmp_path / 'native.py'
    source.write_text('class Callable:\n    __slots__ = ()\n'
                      '    def __call__(self): return None\n'
                      'value = Callable()\n'
                      'def inspect(inputs, context): return {"seen": True}\n')
    definition = load_definition(entry, base_root=tmp_path, pack_root=tmp_path)
    catalog = load_native_catalog(definition, source)
    assert callable(catalog['value'])
    assert native_origin(catalog['inspect']) == (definition.definition_id, source)


def test_explicit_pack_token_and_cross_root_native_source(tmp_path):
    base = tmp_path / 'base'
    pack = tmp_path / 'pack'
    base.mkdir()
    pack.mkdir()
    entry = make_graph(base)
    make_graph(pack)
    entry.write_text(entry.read_text().replace(
        '(workflows/inspect.md.html)', r'(\<resource_pack\>/workflows/inspect.md.html)'))
    node = pack / 'workflows' / 'inspect.md.html'
    node.write_text(node.read_text().replace('[source](../native.py)',
                                             '[source](../../base/native.py)'))

    definition = load_definition(entry, base_root=base, pack_root=pack, entry_root='base')

    assert definition.layout.steps[0].source == base / 'native.py'
    assert any(resource.logical_path == 'pack/workflows/inspect.md.html'
               for resource in definition.resources)


def test_shared_schema_resource_is_captured_once(tmp_path):
    entry = make_graph(tmp_path)
    node = tmp_path / 'workflows' / 'inspect.md.html'
    node.write_text(node.read_text().replace('[output](../schemas/output.json)',
                                             '[output](../schemas/input.json)'))

    definition = load_definition(entry, base_root=tmp_path, pack_root=tmp_path)

    assert len([resource for resource in definition.resources
                if resource.logical_path.endswith('schemas/input.json')]) == 1


def test_step_limit_rejects_extra_sibling_before_file_loading(tmp_path, monkeypatch):
    import api_agent.workflows.definition as module
    entry = make_graph(tmp_path)
    entry.write_text(entry.read_text().replace('## Bindings',
                     '2.  [node second](workflows/second.md.html)\n\n## Bindings'))
    monkeypatch.setattr(module, '_MAX_STEPS', 1)

    with pytest.raises(LayoutError, match='exceeds 1 steps'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path)


def test_file_swap_to_external_symlink_is_rejected_before_read(tmp_path, monkeypatch):
    import api_agent.workflows.definition as module
    entry = make_graph(tmp_path)
    external = tmp_path.parent / 'external-native.py'
    external.write_text('outside bytes')
    source = tmp_path / 'native.py'
    original = module.os.open
    swapped = False
    def racing_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == 'native.py' and not swapped:
            swapped = True
            source.unlink()
            source.symlink_to(external)
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(module.os, 'open', racing_open)

    with pytest.raises(LayoutError, match='unsafe or unavailable resource'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path)
    assert swapped


def test_nonregular_fifo_is_rejected_without_blocking(tmp_path):
    import os
    entry = make_graph(tmp_path)
    source = tmp_path / 'native.py'
    source.unlink()
    os.mkfifo(source)
    with pytest.raises(LayoutError, match='not a regular file'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path)


def test_resource_and_snapshot_limits_reject_oversize_before_execution(tmp_path, monkeypatch):
    import api_agent.workflows.definition as module
    entry = make_graph(tmp_path)
    source = tmp_path / 'native.py'
    source.write_bytes(b'x' * (8 * 1024 * 1024 + 1))
    with pytest.raises(LayoutError, match='exceeds 8 MiB'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path)
    source.write_text('def inspect(inputs, context): return {"seen": True}\n')
    monkeypatch.setattr(module, '_MAX_SNAPSHOT_BYTES', 1)
    with pytest.raises(LayoutError, match='exceeds 64 MiB'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path)


def test_bounded_reader_rejects_invalid_relative_path_and_growth(tmp_path, monkeypatch):
    import api_agent.workflows.definition as module
    from types import SimpleNamespace
    with pytest.raises(LayoutError, match='invalid root-relative path'):
        module._read_bounded(tmp_path, tmp_path)
    source = tmp_path / 'large.txt'
    source.write_bytes(b'x' * (8 * 1024 * 1024 + 1))
    original = module.os.fstat
    def underestimated(fd):
        result = original(fd)
        return SimpleNamespace(st_mode=result.st_mode, st_size=0, st_dev=result.st_dev,
                               st_ino=result.st_ino, st_mtime_ns=result.st_mtime_ns)
    monkeypatch.setattr(module.os, 'fstat', underestimated)
    with pytest.raises(LayoutError, match='exceeds 8 MiB'):
        module._read_bounded(source, tmp_path)


def test_bounded_reader_rejects_change_while_reading(tmp_path, monkeypatch):
    import api_agent.workflows.definition as module
    from types import SimpleNamespace
    source = tmp_path / 'native.py'
    source.write_text('def inspect(): pass\n')
    original = module.os.fstat
    calls = 0
    def changed_metadata(fd):
        nonlocal calls
        calls += 1
        result = original(fd)
        return SimpleNamespace(st_mode=result.st_mode, st_size=result.st_size,
                               st_dev=result.st_dev, st_ino=result.st_ino,
                               st_mtime_ns=result.st_mtime_ns + (1 if calls == 2 else 0))
    monkeypatch.setattr(module.os, 'fstat', changed_metadata)
    with pytest.raises(LayoutError, match='changed during read'):
        module._read_bounded(source, tmp_path)


def test_symlink_to_outside_declared_roots_is_rejected(tmp_path):
    entry = make_graph(tmp_path)
    outside = tmp_path.parent / 'outside-workflow.md.html'
    outside.write_text('# Node v2: inspect\n')
    linked = tmp_path / 'workflows' / 'inspect.md.html'
    linked.unlink()
    linked.symlink_to(outside)
    with pytest.raises(LayoutError, match='outside declared roots'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path)


def test_ancestor_directory_swap_to_external_symlink_is_rejected(tmp_path, monkeypatch):
    import api_agent.workflows.definition as module
    entry = make_graph(tmp_path)
    original = module.os.open
    workflows = tmp_path / 'workflows'
    moved = tmp_path.parent / 'moved-workflows'
    moved.mkdir(exist_ok=True)
    swapped = False
    def racing_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == 'workflows' and not swapped:
            swapped = True
            workflows.rename(moved / 'workflows')
            workflows.symlink_to(moved / 'workflows')
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(module.os, 'open', racing_open)
    with pytest.raises(LayoutError, match='unsafe or unavailable resource'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path)
    assert swapped


def test_workflow_binding_rejects_fifo_after_definition_admission(tmp_path):
    import os
    entry = make_graph(tmp_path)
    definition = load_definition(entry, base_root=tmp_path, pack_root=tmp_path)
    source = tmp_path / 'native.py'
    catalog = load_native_catalog(definition, source)
    source.unlink()
    os.mkfifo(source)
    with pytest.raises(LayoutError, match='not a regular file'):
        Workflow(definition, {'inspect': NodeFunction(catalog['inspect'])})


def test_loader_provenance_rejects_code_object_swap(tmp_path):
    entry = make_graph(tmp_path)
    source = tmp_path / 'native.py'
    old_definition = load_definition(entry, base_root=tmp_path, pack_root=tmp_path)
    old = load_native_catalog(old_definition, source)['inspect']
    source.write_text('def inspect(inputs, context):\n    return {"seen": False}\n')
    current = load_definition(entry, base_root=tmp_path, pack_root=tmp_path)
    fresh = load_native_catalog(current, source)['inspect']
    assert native_origin(fresh) == (current.definition_id, source)
    fresh.__code__ = old.__code__

    assert native_origin(fresh) is None
    with pytest.raises(LayoutError, match='does not belong to the admitted definition'):
        Workflow(current, {'inspect': NodeFunction(fresh)})


def test_inline_prompt_counts_toward_unique_snapshot_limit(tmp_path, monkeypatch):
    import api_agent.workflows.definition as module
    entry = make_graph(tmp_path)
    admitted = load_definition(entry, base_root=tmp_path, pack_root=tmp_path)
    real_bytes = sum(len(resource.content_bytes) for resource in admitted.resources
                     if resource.kind != 'prompt')
    inline_bytes = len(admitted.contents[admitted.layout.tables['Prompts'][0]['File']])
    monkeypatch.setattr(module, '_MAX_SNAPSHOT_BYTES', real_bytes + inline_bytes - 1)

    with pytest.raises(LayoutError, match='workflow snapshot exceeds 64 MiB'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path)


def test_repeated_inline_prompt_reference_counts_bytes_once(tmp_path, monkeypatch):
    import api_agent.workflows.definition as module
    entry = make_graph(tmp_path)
    node = tmp_path / 'workflows' / 'inspect.md.html'
    node.write_text(node.read_text().replace('| inspect | @greeting | always |',
                                             '| inspect | @greeting | always |\n'
                                             '| inspect | @greeting | always |'))
    admitted = load_definition(entry, base_root=tmp_path, pack_root=tmp_path)
    total = sum(len(resource.content_bytes) for resource in admitted.resources)
    monkeypatch.setattr(module, '_MAX_SNAPSHOT_BYTES', total)

    repeated = load_definition(entry, base_root=tmp_path, pack_root=tmp_path)

    assert repeated.definition_id == admitted.definition_id
    assert len([resource for resource in repeated.resources if resource.logical_path.startswith('inline/')]) == 1
