"""Prompt slot and linked prompt files cannot be redirected by a symlink out of their root."""
import os
from pathlib import Path
import shutil

import pytest

from api_agent.workflows.definition import load_definition
from api_agent.workflows.layout import LayoutError
from test_workflow_stateful import fixture_graph  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'tests/fixtures/prompt_slots'


@pytest.fixture
def tree(tmp_path):
    return shutil.copytree(FIXTURE, tmp_path / 'fixture')


def load(tree):
    return load_definition(tree / 'workflow/workflow.md.html', base_root=tree / 'workflow',
                           pack_root=tree / 'packs/fleet')


def relink(path, target):
    path.unlink()
    os.symlink(target, path)


def test_slot_symlinked_to_a_file_outside_its_pack_is_rejected(tree):
    (tree / 'workflow/.env').write_text('API_SECRET=synthetic\n')
    relink(tree / 'packs/fleet/prompts/persona.md', '../../../workflow/.env')

    with pytest.raises(LayoutError, match='prompt file must be a Markdown or text file'):
        load(tree)


def test_slot_symlinked_to_markdown_in_the_library_root_is_rejected(tree):
    relink(tree / 'packs/fleet/prompts/persona.md', '../../../workflow/prompts/system.md')

    with pytest.raises(LayoutError, match='prompt file must stay inside its resource root'):
        load(tree)


def test_slot_symlinked_to_a_file_with_another_suffix_inside_its_pack_is_rejected(tree):
    (tree / 'packs/fleet/settings.env').write_text('API_SECRET=synthetic\n')
    relink(tree / 'packs/fleet/prompts/persona.md', '../settings.env')

    with pytest.raises(LayoutError, match='prompt file must be a Markdown or text file'):
        load(tree)


def test_slot_symlinked_within_its_pack_to_text_keeps_the_target_text(tree):
    (tree / 'packs/fleet/shared.txt').write_text('a shared persona\n')
    relink(tree / 'packs/fleet/prompts/persona.md', '../shared.txt')

    definition = load(tree)

    assert definition.contents[definition.slot_files['persona']] == b'a shared persona\n'


def test_linked_prompt_symlinked_into_another_root_is_rejected(tree):
    relink(tree / 'workflow/prompts/system.md', '../../packs/fleet/prompts/rules.md')

    with pytest.raises(LayoutError, match='prompt file must stay inside its resource root'):
        load(tree)


def test_linked_prompt_symlinked_within_its_root_to_markdown_is_admitted(tree):
    shutil.copy(tree / 'workflow/prompts/system.md', tree / 'workflow/prompts/real.md')
    relink(tree / 'workflow/prompts/system.md', 'real.md')

    definition = load(tree)

    assert any(item.logical_path == 'base/prompts/real.md' and item.kind == 'prompt'
               for item in definition.resources)


def test_typed_prompt_symlinked_out_of_its_pack_is_rejected(fixture_graph):
    from test_workflow_stateful import _PROMPTED_INTAKE, _SLOTS, _prompted
    root, entry = fixture_graph
    _prompted(root, entry, _PROMPTED_INTAKE.replace('(prompts/extra.md)', '(<resource_pack>/prompts/extra.md)'), _SLOTS)
    pack = root / 'packdir'
    (pack / 'prompts').mkdir(parents=True)
    (root / 'outside.md').write_text('Not part of the pack.\n')
    os.symlink('../../outside.md', pack / 'prompts/extra.md')

    with pytest.raises(LayoutError, match='prompt file must stay inside its resource root'):
        load_definition(entry, base_root=root, pack_root=pack)
