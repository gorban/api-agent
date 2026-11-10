"""Saved diagram links name their recorded repository and an existing target."""
from pathlib import Path

import pytest

from api_agent.workflows import viewer_build as vb
from api_agent.workflows.viewer_render import RenderError

ROOT = Path(__file__).resolve().parents[2]
PREFIX = '//github.com/example/agent/blob/_____switch_branch_____/'


def _owner(tmp_path, links, *, config='version = 1\nrepository_url = "//github.com/example/agent"\n'):
    (tmp_path / 'cfg').mkdir(parents=True)
    (tmp_path / 'cfg/workflow-view.toml').write_text(config)
    (tmp_path / 'workflows').mkdir()
    (tmp_path / 'workflows/agent.md.html').write_text('# Agent\n')
    diagrams = tmp_path / '.private_resources/diagrams'
    diagrams.mkdir(parents=True)
    anchors = ''.join(f'<a href="{link}"><text>node</text></a>' for link in links)
    (diagrams / 'flow.svg').write_text(f'<svg xmlns="http://www.w3.org/2000/svg">{anchors}</svg>')
    return tmp_path


def test_links_to_existing_targets_of_the_recorded_repository_pass(tmp_path):
    owner = _owner(tmp_path, [PREFIX + 'workflows/agent.md.html#agent', '#local-anchor'])

    vb.check_saved_diagram_links((owner,))


@pytest.mark.parametrize('link, problem', [
    (PREFIX + 'workflows/missing.md.html', 'missing target'),
    ('//github.com/other-owner/agent/blob/_____switch_branch_____/workflows/agent.md.html', 'recorded repository'),
    ('//github.com/example/agent/blob/main/workflows/agent.md.html', 'recorded repository'),
])
def test_links_that_cannot_resolve_are_rejected(tmp_path, link, problem):
    owner = _owner(tmp_path, [link])

    with pytest.raises(RenderError, match=problem):
        vb.check_saved_diagram_links((owner,))


def test_links_into_another_recorded_owner_resolve_there(tmp_path):
    library = tmp_path / 'library'
    (library / 'cfg').mkdir(parents=True)
    (library / 'cfg/workflow-view.toml').write_text('version = 1\nrepository_url = "//github.com/example/library"\n')
    (library / 'workflows').mkdir()
    (library / 'workflows/shared.md.html').write_text('# Shared\n')
    link = '//github.com/example/library/blob/_____switch_branch_____/workflows/shared.md.html'
    owner = _owner(tmp_path / 'pack', [link])

    vb.check_saved_diagram_links((owner, library))
    with pytest.raises(RenderError, match='recorded repository'):
        vb.check_saved_diagram_links((owner,))


def test_an_owner_without_saved_diagrams_or_identity_is_skipped(tmp_path):
    vb.check_saved_diagram_links((tmp_path,))


def test_this_repository_saved_diagrams_link_to_existing_targets():
    vb.check_saved_diagram_links((ROOT,))


def test_check_views_verifies_saved_diagram_links(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(vb, 'check_saved_diagram_links', lambda owners: seen.append(owners))
    for name in ('discover_view_entries', 'read_view_roots', '_projections', '_check_inventory_files'):
        monkeypatch.setattr(vb, name, lambda *args, **kwargs: ())
    monkeypatch.setattr(vb, '_inventory_contexts', lambda *args: ())
    monkeypatch.setattr(vb, '_repository_inputs', lambda root: ({}, ()))
    monkeypatch.setattr(vb, '_owner_records', lambda *args: ({}, {}))
    monkeypatch.setattr(vb, '_inventory_files', lambda *args, **kwargs: ())

    vb.check_views(tmp_path)

    assert seen == [(tmp_path.resolve(),)]
