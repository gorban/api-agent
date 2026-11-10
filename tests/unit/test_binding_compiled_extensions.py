"""A compiled binding plan carries the declared Application and pack-supplied prompt slots."""
import asyncio
import base64
import json
from pathlib import Path, PurePosixPath
import shutil

import pytest

from artifact_acceptance_support import build_wheel, install
from artifact_unit_support import Built, edited, isolated_imports, load_built  # noqa: F401
from test_binding_application import FACTORY, SECTION
from test_binding_prompt_slots import system_prompt
from test_workflow_definition import make_graph

from api_agent.providers.llm import LLMResult, Message, TextContent, Usage
from api_agent.runtime.local_tools import selected_application
from api_agent.workflows import artifact
from api_agent.workflows.artifact import ArtifactRejected, CompiledDefinition, plan_digest
from api_agent.workflows.artifact_compile import compile_plan
from api_agent.workflows.definition import load_definition


def build(tmp_path, *, application=True):
    root = tmp_path / 'tree'
    root.mkdir()
    entry = make_graph(root)
    files = {'aq_binding/__init__.py': b'', 'aq_binding/native.py': (root / 'native.py').read_bytes()}
    if application:
        (root / 'application.py').write_text(FACTORY)
        entry.write_text(entry.read_text().replace('## Flow', SECTION))
        files['aq_binding/application.py'] = FACTORY.encode()
    wheel = build_wheel(tmp_path, 'aq-binding', files)
    plan = compile_plan(entry, base_root=root, pack_root=root, wheels=[wheel])
    return Built(plan, plan_digest(plan), install(wheel, tmp_path / 'site'), wheel, tmp_path)


@pytest.fixture
def built(tmp_path):
    return build(tmp_path)


def test_plan_without_application_keeps_the_original_sections(tmp_path):
    built = build(tmp_path, application=False)

    assert 'application_binding' not in json.loads(built.plan)
    assert load_built(built).application_binding is None


def test_compiled_application_round_trips_and_is_selected(built):
    definition = load_built(built)

    assert isinstance(definition, CompiledDefinition)
    assert definition.application_binding == ('create_application', PurePosixPath('/artifact/base/application.py'))
    application = selected_application(definition, config={'KEY': 1})
    assert application.definition is definition
    assert application.config == {'KEY': 1}


def test_compiled_application_source_is_a_hashed_binding_not_embedded(built):
    value = json.loads(built.plan)

    assert 'base/application.py' in {row['source'] for row in value['bindings']}
    assert 'def create_application' not in built.plan.decode()


@pytest.mark.parametrize('encoded,message', [
    (['create_application', {'$p': 'base/native.py'}], 'does not define declared binding create_application'),
    (['bad-name', {'$p': 'base/application.py'}], 'invalid application binding'),
    (['create_application', {'$p': 'base/missing.py'}], 'invalid application binding'),
    ('create_application', 'invalid application binding'),
    (['create_other', {'$p': 'base/application.py'}], 'does not define declared binding create_other'),
])
def test_invalid_application_plan_rows_are_rejected(built, encoded, message):
    plan = edited(built.plan, lambda value: value.update(application_binding=encoded))

    with pytest.raises(ArtifactRejected, match=message):
        load_built(built, plan)


def test_application_binding_is_not_valid_beside_typed_state(built):
    plan = edited(built.plan, lambda value: value.update(state={}))

    with pytest.raises(ArtifactRejected, match='missing or unknown sections'):
        load_built(built, plan)


def test_unknown_plan_sections_are_still_rejected(built):
    plan = edited(built.plan, lambda value: value.update(surprise=1))

    with pytest.raises(ArtifactRejected, match='missing or unknown sections'):
        load_built(built, plan)


SLOTS = Path(__file__).resolve().parents[1] / 'fixtures/prompt_slots'


def build_slots(tmp_path, pack='fleet', *, application=False):
    tree = tmp_path / 'tree'
    shutil.copytree(SLOTS, tree)
    entry = tree / 'workflow/workflow.md.html'
    files = {'aq_slots/__init__.py': b'', 'aq_slots/functions.py': (tree / 'workflow/functions.py').read_bytes()}
    if application:
        (tree / 'workflow/application.py').write_text(FACTORY)
        entry.write_text(entry.read_text().replace('## Flow', SECTION))
        files['aq_slots/application.py'] = FACTORY.encode()
    wheel = build_wheel(tmp_path, 'aq-slots', files)
    plan = compile_plan(entry, base_root=tree / 'workflow', pack_root=tree / 'packs' / pack, wheels=[wheel])
    return Built(plan, plan_digest(plan), install(wheel, tmp_path / 'site'), wheel, tmp_path)


def compiled_system_prompt(definition):
    requests = []

    async def llm(request):
        requests.append(request)
        return LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))

    workflow = definition.workflow(services={'llm': llm, 'selection': 'fixture/model'})
    assert asyncio.run(workflow.arun({'question': 'Which?'}))['answer'] == {'answer': 'done'}
    return requests[0].payload.messages[0].content[0].text


@pytest.fixture
def slots_built(tmp_path):
    return build_slots(tmp_path)


def test_compiled_plan_fills_each_packs_slots_without_the_source_tree(tmp_path):
    fleet = load_built(build_slots(tmp_path / 'one'))
    bakery = load_built(build_slots(tmp_path / 'two', 'bakery'))

    assert sorted(fleet.slot_files) == ['closing', 'persona', 'rules']
    assert fleet.slot_files['persona'] == PurePosixPath('/artifact/pack/prompts/persona.md')
    assert compiled_system_prompt(fleet).startswith('You are a fleet equipment management assistant.\n')
    assert compiled_system_prompt(bakery).endswith('Sign off with: the order number\n')
    assert fleet.definition_id != bakery.definition_id


def test_compiled_slots_match_the_source_definition_identity(tmp_path):
    built = build_slots(tmp_path)
    source = load_definition(tmp_path / 'tree/workflow/workflow.md.html', base_root=tmp_path / 'tree/workflow',
                             pack_root=tmp_path / 'tree/packs/fleet')

    assert json.loads(built.plan)['definition_id'] == source.definition_id
    assert compiled_system_prompt(load_built(built)) == system_prompt(source)


def test_slot_texts_are_plan_data_not_source(slots_built):
    value = json.loads(slots_built.plan)

    assert value['prompt_slots'] == {'$m': [
        ['closing', {'$p': 'pack/prompts/closing.txt'}], ['persona', {'$p': 'pack/prompts/persona.md'}],
        ['rules', {'$p': 'pack/prompts/rules.md'}]]}
    assert {row['path'] for row in value['resources'] if row['kind'] == 'prompt'} >= {
        'pack/prompts/persona.md', 'pack/prompts/rules.md', 'pack/prompts/closing.txt'}


def test_plan_without_slots_has_no_slot_section(built):
    assert 'prompt_slots' not in json.loads(built.plan)
    assert load_built(built).slot_files == {}


def test_application_and_slots_travel_in_one_plan(tmp_path):
    definition = load_built(build_slots(tmp_path, application=True))

    assert definition.application_binding[0] == 'create_application'
    assert sorted(definition.slot_files) == ['closing', 'persona', 'rules']
    assert selected_application(definition).definition is definition


@pytest.mark.parametrize('change,message', [
    (lambda value: value.pop('prompt_slots'), 'prompt slots differ'),
    (lambda value: value.update(prompt_slots={'$m': []}), 'invalid prompt slots'),
    (lambda value: value.update(prompt_slots={'$m': [['Bad-Name', {'$p': 'pack/prompts/persona.md'}]]}),
     'invalid prompt slots'),
    (lambda value: value.update(prompt_slots={'$m': [['persona', {'$p': 'pack/prompts/missing.md'}]]}),
     'invalid prompt slots'),
    (lambda value: value.update(prompt_slots={'$m': [['persona', {'$p': 'base/schemas/input.json'}]]}),
     'invalid prompt slots'),
    (lambda value: value.update(prompt_slots={'$m': [['persona', {'$p': 'pack/prompts/persona.md'}]]}),
     'prompt slots differ'),
])
def test_invalid_slot_sections_are_rejected(slots_built, change, message):
    with pytest.raises(ArtifactRejected, match=message):
        load_built(slots_built, edited(slots_built.plan, change))


def test_slot_section_must_be_a_mapping(slots_built):
    value = json.loads(slots_built.plan)
    value['prompt_slots'] = 'persona'
    value['compiled_plan_identity'] = artifact.compiled_plan_identity(value)

    with pytest.raises(ArtifactRejected, match='invalid prompt slots'):
        load_built(slots_built, artifact.canonical_json(value))


def test_slot_text_must_be_utf8(slots_built):
    def corrupt(value):
        row = next(item for item in value['resources'] if item['path'] == 'pack/prompts/persona.md')
        row['content']['b64'] = base64.b64encode(b'\xff\xfe').decode()
    with pytest.raises(ArtifactRejected, match='prompt slot text must be UTF-8'):
        load_built(slots_built, edited(slots_built.plan, corrupt))


def test_the_slot_mapping_is_part_of_the_definition_identity(slots_built):
    value = json.loads(slots_built.plan)
    mapping = dict(value['prompt_slots']['$m'])
    mapping['persona'], mapping['rules'] = mapping['rules'], mapping['persona']
    value['prompt_slots'] = {'$m': sorted(mapping.items())}
    value['compiled_plan_identity'] = artifact.compiled_plan_identity(value)

    with pytest.raises(ArtifactRejected, match='definition_id does not match'):
        load_built(slots_built, artifact.canonical_json(value))
