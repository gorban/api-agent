"""A selecting pack's prompt slots take precedence over the library workflow's own pack."""
import asyncio
import json
from pathlib import Path
import shutil

import pytest

from artifact_acceptance_support import build_wheel, install
from artifact_unit_support import Built, isolated_imports, load_built  # noqa: F401
from test_binding_compiled_extensions import compiled_system_prompt
from test_binding_prompt_slots import FIXTURE, system_prompt

from api_agent.runtime.resource_pack import load_launch_context, load_selected_definition
from api_agent.workflows.artifact import plan_digest
from api_agent.workflows.artifact_compile import compile_plan
from api_agent.workflows.definition import load_definition
from api_agent.workflows.identity import declaration_identity, logical_resolver
from api_agent.workflows.layout import LayoutError

ROOT = Path(__file__).resolve().parents[2]
FLEET = {'persona': 'a fleet equipment management assistant',
         'rules': '- Report equipment by unit number.\n- Prefer the most recent inspection.',
         'closing': 'the unit number'}
FILES = {'persona': 'persona.md', 'rules': 'rules.md', 'closing': 'closing.txt'}


@pytest.fixture
def tree(tmp_path):
    """The workflow as a library whose own root ships default slot text, plus a pack directory."""
    root = shutil.copytree(FIXTURE / 'workflow', tmp_path / 'library')
    (root / 'slots').mkdir()
    for name in FILES.values():
        shutil.copy2(FIXTURE / 'packs/fleet/prompts' / name, root / 'slots' / name)
    manifest(root, FILES, 'slots')
    return tmp_path


def manifest(directory, slots, folder='prompts'):
    rows = ''.join(f'{slot} = "{folder}/{FILES[slot]}"\n' for slot in slots)
    (directory / 'pack.toml').write_text(f'[prompt_slots]\n{rows}')


def product(tree, name='product', **texts):
    """A selecting pack supplying only the named slots, each with the given text."""
    pack = tree / name
    (pack / 'prompts').mkdir(parents=True)
    for slot, text in texts.items():
        (pack / 'prompts' / FILES[slot]).write_text(text + '\n')
    manifest(pack, texts)
    return pack


def load(tree, pack, *, entry=None, entry_root=None):
    library = tree / 'library'
    pack.mkdir(exist_ok=True)
    return load_definition(entry or library / 'workflow.md.html', base_root=library, pack_root=pack,
                           entry_root=entry_root)


def logical_paths(definition):
    logical = logical_resolver(definition)
    return {slot: logical(path) for slot, path in definition.slot_files.items()}


def prompt_for(definition):
    return system_prompt(definition)


def test_the_library_root_supplies_its_own_default_slots(tree):
    owner = tree / 'library'
    expected = prompt_for(load_definition(FIXTURE / 'workflow/workflow.md.html', base_root=FIXTURE / 'workflow',
                                          pack_root=FIXTURE / 'packs/fleet'))
    alone = load_definition(owner / 'workflow.md.html', base_root=owner, pack_root=owner)
    inherited = load(tree, tree / 'empty_pack')

    assert prompt_for(alone) == prompt_for(inherited) == expected
    assert {path.split('/')[0] for path in logical_paths(alone).values()} == {'pack'}
    assert {path.split('/')[0] for path in logical_paths(inherited).values()} == {'base'}


def test_the_selected_pack_overrides_one_slot_and_inherits_the_rest(tree):
    pack = product(tree, persona='a delivery planning assistant')

    definition = load(tree, pack)

    assert prompt_for(definition).startswith('You are a delivery planning assistant.\nDomain rules:\n- Report equipment')
    assert logical_paths(definition) == {'persona': 'pack/prompts/persona.md', 'rules': 'base/slots/rules.md',
                                         'closing': 'base/slots/closing.txt'}


def test_the_selected_pack_supplies_a_slot_the_library_lacks(tree):
    manifest(tree / 'library', ['persona', 'rules'], 'slots')
    pack = product(tree, closing='the route number')

    definition = load(tree, pack)

    assert prompt_for(definition).endswith('Sign off with: the route number\n')
    assert logical_paths(definition)['closing'] == 'pack/prompts/closing.txt'


def test_a_slot_missing_everywhere_lists_every_consulted_pack(tree):
    manifest(tree / 'library', ['persona', 'rules'], 'slots')
    pack = product(tree, persona='a delivery planning assistant')

    with pytest.raises(LayoutError) as error:
        load(tree, pack)

    message = str(error.value)
    assert 'answer.md.html:26: prompt uses slot closing that no consulted resource pack supplies' in message
    assert f'pack {pack}' in message and f'base {tree / "library"}' in message
    assert message.index(f'pack {pack}') < message.index('base ')


def test_one_directory_is_consulted_once(tree):
    manifest(tree / 'library', ['persona', 'rules'], 'slots')
    library = tree / 'library'

    with pytest.raises(LayoutError) as error:
        load_definition(library / 'workflow.md.html', base_root=library, pack_root=library)

    assert f'pack {library}' in str(error.value) and 'base ' not in str(error.value)


def test_identity_follows_the_resolved_slot_mapping(tree):
    demo = load(tree, tree / 'demo')
    first = load(tree, product(tree, 'first', persona='a delivery planning assistant'))
    second = load(tree, product(tree, 'second', persona='a delivery planning assistant'))
    other = load(tree, product(tree, 'other', persona='a dispatch assistant'))
    same_text = load(tree, product(tree, 'same_text', **FLEET))

    assert first.definition_id == second.definition_id
    assert declaration_identity(first) == declaration_identity(second)
    assert len({demo.definition_id, first.definition_id, other.definition_id}) == 3
    assert len({declaration_identity(demo), declaration_identity(first), declaration_identity(other)}) == 3
    # Identical text still records which pack supplied it.
    assert same_text.definition_id != demo.definition_id
    assert declaration_identity(same_text) != declaration_identity(demo)


def test_an_import_wrapper_in_the_selecting_pack_uses_the_same_precedence(tree):
    pack = product(tree, persona='a delivery planning assistant')
    wrapper = pack / 'flow.md.html'
    wrapper.write_text('# Workflow\n\n## Graph ID\n\n| ID |\n| --- |\n| product |\n\n'
                       '## Import\n\n| Root | Path |\n| --- | --- |\n| base | workflow.md.html |\n')

    definition = load(tree, pack, entry=wrapper, entry_root='pack')

    assert definition.entry_root == 'pack'
    assert prompt_for(definition).startswith('You are a delivery planning assistant.\n')
    assert logical_paths(definition)['rules'] == 'base/slots/rules.md'
    assert definition.definition_id != load(tree, tree / 'demo').definition_id


def test_a_pack_selecting_a_workflow_library_resolves_slots_through_its_manifest(tree):
    pack = product(tree, persona='a delivery planning assistant')
    (pack / 'cfg').mkdir()
    (pack / 'cfg/config.toml').write_text('version = 1\n')
    (pack / 'flow.md.html').write_text('# Workflow\n\n## Graph ID\n\n| ID |\n| --- |\n| product |\n\n'
                                       '## Import\n\n| Root | Path |\n| --- | --- |\n| base | workflow.md.html |\n')
    (pack / 'pack.toml').write_text(
        'version = 1\nid = "product"\nconfig = "cfg/config.toml"\n'
        '[workflow]\nversion = 1\nroot = "pack"\nentry = "flow.md.html"\n'
        '[workflow.library]\nsource = "../library"\ndistribution = "shared"\nresource = "workflows"\n'
        '[prompt_slots]\npersona = "prompts/persona.md"\n')

    definition = load_selected_definition(load_launch_context(ROOT, pack))

    assert prompt_for(definition).startswith('You are a delivery planning assistant.\nDomain rules:\n- Report')
    assert logical_paths(definition)['closing'] == 'base/slots/closing.txt'


def test_a_plan_compiled_under_the_product_pack_carries_the_product_slot_text(tree):
    library = tree / 'library'
    pack = product(tree, persona='a delivery planning assistant')
    files = {'aq_slots/__init__.py': b'', 'aq_slots/functions.py': (library / 'functions.py').read_bytes()}
    wheel = build_wheel(tree, 'aq-slots', files)
    plan = compile_plan(library / 'workflow.md.html', base_root=library, pack_root=pack, wheels=[wheel])
    demo_plan = compile_plan(library / 'workflow.md.html', base_root=library, pack_root=tree / 'demo', wheels=[wheel])
    built = Built(plan, plan_digest(plan), install(wheel, tree / 'site'), wheel, tree)

    value = json.loads(plan)
    definition = load_built(built)

    assert value['prompt_slots'] == {'$m': [['closing', {'$p': 'base/slots/closing.txt'}],
                                            ['persona', {'$p': 'pack/prompts/persona.md'}],
                                            ['rules', {'$p': 'base/slots/rules.md'}]]}
    assert compiled_system_prompt(definition).startswith('You are a delivery planning assistant.\n')
    assert json.loads(demo_plan)['definition_id'] != value['definition_id']
    assert value['definition_id'] == load(tree, pack).definition_id
