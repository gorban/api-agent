"""A resource pack supplies the text of ``{{slot}}`` placeholders in binding-grammar prompts."""
import asyncio
from pathlib import Path
import shutil

import pytest

from api_agent.providers.llm import LLMResult, Message, TextContent, Usage
from api_agent.runtime.resource_pack import ResourcePackError, load_launch_context
from api_agent.workflows.definition import load_definition, load_native_catalog
from api_agent.workflows.execution import NodeFunction, Workflow
from api_agent.workflows.identity import declaration_identity
from api_agent.workflows.layout import LayoutError
from api_agent.workflows.viewer_projection import project_view

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'tests/fixtures/prompt_slots'
SYSTEM = ("You are {persona}.\nDomain rules:\n{rules}\n"
          "Answer the user's question in one short paragraph.\n\n\nSign off with: {closing}\n")


@pytest.fixture
def tree(tmp_path):
    """A mutable copy of the workflow and both fixture packs."""
    return shutil.copytree(FIXTURE, tmp_path / 'fixture')


def load(tree, pack='fleet'):
    return load_definition(tree / 'workflow/workflow.md.html', base_root=tree / 'workflow',
                           pack_root=tree / 'packs' / pack)


def system_prompt(definition):
    """Run the node once against a controlled model service; return its system message."""
    requests = []

    async def llm(request):
        requests.append(request)
        return LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))

    source = definition.layout.steps[0].source
    workflow = Workflow(definition, {'answer': NodeFunction(load_native_catalog(definition, source)['answer'], 'llm')},
                        services={'llm': llm, 'selection': 'fixture/model'})
    assert asyncio.run(workflow.arun({'question': 'Which?'}))['answer'] == {'answer': 'done'}
    assert requests[0].payload.messages[1].content[0].text == 'Which?'
    return requests[0].payload.messages[0].content[0].text


def test_each_pack_fills_the_same_workflow_with_its_own_slot_text():
    fleet = system_prompt(load(FIXTURE))
    bakery = system_prompt(load(FIXTURE, 'bakery'))

    assert fleet == SYSTEM.format(
        persona='a fleet equipment management assistant',
        rules='- Report equipment by unit number.\n- Prefer the most recent inspection.',
        closing='the unit number')
    assert bakery == SYSTEM.format(
        persona='a bakery operations assistant',
        rules='- Follow the user request.', closing='the order number')
    assert fleet != bakery


def test_slot_files_are_captured_as_prompt_resources():
    definition = load(FIXTURE)

    assert sorted(definition.slot_files) == ['closing', 'persona', 'rules']
    assert definition.contents[definition.slot_files['persona']] == b'a fleet equipment management assistant\n'
    kinds = {item.logical_path: item.kind for item in definition.resources}
    assert kinds['pack/prompts/persona.md'] == kinds['pack/prompts/closing.txt'] == 'prompt'
    assert definition.layout.files and definition.slot_files['rules'] in definition.layout.files


def test_a_slot_the_pack_does_not_supply_names_the_slot_and_prompt_location(tree):
    pack = tree / 'packs/fleet'
    manifest = pack / 'pack.toml'
    manifest.write_text(manifest.read_text().replace('closing = "prompts/closing.txt"\n', ''))

    with pytest.raises(LayoutError, match=r"answer\.md\.html:26: prompt uses slot closing .*does not supply"):
        load(tree)


def test_a_pack_without_a_manifest_supplies_no_slots(tree):
    (tree / 'packs/fleet/pack.toml').unlink()

    with pytest.raises(LayoutError, match='slot persona'):
        load(tree)


def test_prompts_without_slots_need_no_manifest(tree):
    (tree / 'packs/fleet/pack.toml').unlink()
    (tree / 'workflow/prompts/system.md').write_text('No slots here.\n')
    node = tree / 'workflow/answer.md.html'
    node.write_text(node.read_text().replace('Sign off with: {{closing}}', 'Sign off.'))

    definition = load(tree)

    assert definition.slot_files == {}


def test_a_supplied_but_unused_slot_is_allowed_and_not_captured(tree):
    pack = tree / 'packs/fleet'
    with (pack / 'pack.toml').open('a') as stream:
        stream.write('unused = "prompts/unused.md"\n')
    (pack / 'prompts/unused.md').write_text('never referenced\n')

    definition = load(tree)

    assert 'unused' not in definition.slot_files
    assert 'pack/prompts/unused.md' not in {item.logical_path for item in definition.resources}
    assert definition.definition_id == load(FIXTURE).definition_id


def test_slot_text_is_never_scanned_for_placeholders(tree):
    pack = tree / 'packs/fleet'
    (pack / 'prompts/persona.md').write_text('an assistant that mentions {{rules}} and {{missing}}\n')

    prompt = system_prompt(load(tree))

    assert prompt.startswith('You are an assistant that mentions {{rules}} and {{missing}}.\n')


def test_a_backslash_escapes_a_literal_placeholder(tree):
    prompts = tree / 'workflow/prompts/system.md'
    prompts.write_text('Write \\{{persona}} for {{persona}}; {{ other }} and {{Not_A_Slot}} stay as written.\n')
    node = tree / 'workflow/answer.md.html'
    node.write_text(node.read_text().replace('Sign off with: {{closing}}', 'Sign off.'))

    prompt = system_prompt(load(tree))

    assert prompt == ('Write {{persona}} for a fleet equipment management assistant; '
                      '{{ other }} and {{Not_A_Slot}} stay as written.\n\n\nSign off.\n')


def test_an_escaped_placeholder_needs_no_supplied_slot(tree):
    (tree / 'packs/fleet/pack.toml').unlink()
    (tree / 'workflow/prompts/system.md').write_text('Literal \\{{persona}} only.\n')
    node = tree / 'workflow/answer.md.html'
    node.write_text(node.read_text().replace('Sign off with: {{closing}}', 'Sign off.'))

    assert load(tree).slot_files == {}


def test_a_slot_used_by_several_prompts_is_captured_once(tree):
    node = tree / 'workflow/answer.md.html'
    node.write_text(node.read_text().replace('Sign off with: {{closing}}', 'As {{persona}}, sign off.'))

    prompt = system_prompt(load(tree))

    assert prompt.count('a fleet equipment management assistant') == 2
    assert sorted(load(tree).slot_files) == ['persona', 'rules']


def test_an_imported_workflow_without_slots_has_none(tree):
    (tree / 'workflow/prompts/system.md').write_text('No slots.\n')
    node = tree / 'workflow/answer.md.html'
    node.write_text(node.read_text().replace('Sign off with: {{closing}}', 'Sign off.'))
    wrapper = tree / 'workflow/flow.md.html'
    wrapper.write_text('# Workflow\n\n## Graph ID\n\n| ID |\n| --- |\n| wrapped |\n\n'
                       '## Import\n\n| Root | Path |\n| --- | --- |\n| base | workflow.md.html |\n')

    definition = load_definition(wrapper, base_root=tree / 'workflow', pack_root=tree / 'packs/fleet')

    assert definition.slot_files == {}


def test_slot_text_changes_the_workflow_identity(tree):
    before = load(tree)
    (tree / 'packs/fleet/prompts/persona.md').write_text('a different persona\n')
    after = load(tree)

    assert before.definition_id != after.definition_id
    assert declaration_identity(before) != declaration_identity(after)
    assert load(FIXTURE, 'fleet').definition_id != load(FIXTURE, 'bakery').definition_id


def swap_persona_and_rules(tree):
    manifest = tree / 'packs/fleet/pack.toml'
    manifest.write_text(manifest.read_text().replace('prompts/persona.md', 'prompts/TMP')
                        .replace('prompts/rules.md', 'prompts/persona.md').replace('prompts/TMP', 'prompts/rules.md'))


def test_swapping_two_slot_files_changes_the_identity_though_the_resources_match(tree):
    before = load(tree)
    swap_persona_and_rules(tree)
    after = load(tree)

    assert {item.logical_path: item.sha256 for item in before.resources} == {
        item.logical_path: item.sha256 for item in after.resources}
    assert before.definition_id != after.definition_id
    assert declaration_identity(before) != declaration_identity(after)


@pytest.mark.parametrize('content,message', [
    ('[prompt_slots]\npersona = 3\n', 'invalid prompt slot persona'),
    ('[prompt_slots]\nBad-Name = "prompts/persona.md"\n', 'invalid prompt slot Bad-Name'),
    ('[prompt_slots]\npersona = "../outside.md"\n', 'invalid prompt slot persona'),
    ('[prompt_slots]\npersona = "/etc/hosts"\n', 'invalid prompt slot persona'),
    ('[prompt_slots]\npersona = "prompts\\\\persona.md"\n', 'invalid prompt slot persona'),
    ('[prompt_slots]\npersona = "prompts/persona.py"\n', 'invalid prompt slot persona'),
    ('prompt_slots = "persona"\n', 'prompt_slots must be a table'),
    ('prompt_slots = [\n', 'invalid resource-pack declaration'),
])
def test_invalid_pack_declarations_are_rejected_with_the_manifest_location(tree, content, message):
    (tree / 'packs/fleet/pack.toml').write_text(content)

    with pytest.raises(LayoutError, match=rf'pack\.toml: {message}'):
        load(tree)


def test_a_missing_slot_file_is_rejected(tree):
    (tree / 'packs/fleet/prompts/persona.md').unlink()

    with pytest.raises(LayoutError, match=r'persona\.md:1: unsafe or unavailable resource'):
        load(tree)


def test_slot_text_must_be_utf8(tree):
    (tree / 'packs/fleet/prompts/persona.md').write_bytes(b'\xff\xfe')

    with pytest.raises(LayoutError, match=r'persona\.md:1: prompt slot text must be UTF-8'):
        load(tree)


def test_the_viewer_names_slots_and_links_the_supplied_files():
    projection = project_view(load(FIXTURE))

    prompts = [row for row in projection.references if row.kind == 'prompts']
    assert [dict(row.attributes).get('slots') for row in prompts] == ['persona,rules', 'closing']
    links = [(dict(row.attributes)['slot'], row.target_path, row.owner_id)
             for row in projection.references if row.kind == 'prompt_slot']
    assert links == [('persona', 'pack/prompts/persona.md', 'answer'),
                     ('rules', 'pack/prompts/rules.md', 'answer'),
                     ('closing', 'pack/prompts/closing.txt', 'answer')]
    assert all(row.target_sha256 for row in projection.references if row.kind == 'prompt_slot')


def test_the_viewer_projection_changes_with_the_slot_text(tree):
    before = project_view(load(tree)).projection_sha256
    (tree / 'packs/fleet/prompts/persona.md').write_text('another persona\n')

    assert project_view(load(tree)).projection_sha256 != before


def test_an_imported_workflow_keeps_its_slots_in_its_identity(tree):
    wrapper = tree / 'workflow/flow.md.html'
    wrapper.write_text('# Workflow\n\n## Graph ID\n\n| ID |\n| --- |\n| wrapped |\n\n'
                       '## Import\n\n| Root | Path |\n| --- | --- |\n| base | workflow.md.html |\n')

    def wrapped():
        return load_definition(wrapper, base_root=tree / 'workflow', pack_root=tree / 'packs/fleet')

    before = wrapped()
    swap_persona_and_rules(tree)
    after = wrapped()

    assert sorted(before.slot_files) == ['closing', 'persona', 'rules']
    assert before.definition_id != after.definition_id


def test_fixture_packs_are_valid_launch_packs_declaring_the_same_slots():
    contexts = [load_launch_context(ROOT, FIXTURE / 'packs' / name) for name in ('fleet', 'bakery')]

    assert [context.pack_id for context in contexts] == ['prompt-slots-fleet', 'prompt-slots-bakery']


@pytest.mark.parametrize('declaration,message', [
    ('prompt_slots = "persona"\n', 'prompt_slots must be a table'),
    ('[prompt_slots]\npersona = "prompts/missing.md"\n', 'prompt_slots.persona must exist'),
    ('[prompt_slots]\npersona = "../escape.md"\n', 'invalid prompt slot persona'),
])
def test_launch_context_rejects_invalid_prompt_slot_declarations(tree, declaration, message):
    pack = tree / 'packs/fleet'
    (pack / 'cfg').mkdir()
    (pack / 'cfg/config.toml').write_text('version = 1\n')
    (pack / 'pack.toml').write_text('version = 1\nid = "slots"\nconfig = "cfg/config.toml"\n' + declaration)

    with pytest.raises(ResourcePackError, match=message):
        load_launch_context(ROOT, pack)


def test_launch_context_without_prompt_slots_is_unchanged(tree):
    pack = tree / 'packs/fleet'
    (pack / 'cfg').mkdir()
    (pack / 'cfg/config.toml').write_text('version = 1\n')
    (pack / 'pack.toml').write_text('version = 1\nid = "plain"\nconfig = "cfg/config.toml"\n')

    assert load_launch_context(ROOT, pack).pack_id == 'plain'
