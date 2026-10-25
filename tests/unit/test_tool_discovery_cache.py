"""Definition reuse must preserve fresh sources, isolation and admission errors."""
from dataclasses import replace
import os
from pathlib import Path

import pytest

from api_agent.config.session import ConfigSession
from api_agent.runtime.resource_pack import load_selected_definition
from api_agent.tooling.discovery_cache import DefinitionCache
from test_tool_selection import selection_fixture, graph
from test_workflow_definition import make_graph


@pytest.fixture
def counted_loads(monkeypatch):
    calls = []
    def load(context):
        calls.append(context.workflow_entry)
        return load_selected_definition(context)
    monkeypatch.setattr('api_agent.tooling.discovery_cache.load_selected_definition', load)
    return calls


def test_unchanged_definition_is_parsed_once(selection_fixture, counted_loads):
    context, _ = selection_fixture
    cache = DefinitionCache()

    first = cache(context)
    again = cache(context)

    assert again is first
    assert len(counted_loads) == 1


def test_inline_prompts_and_pack_links_keep_their_distinct_resource_roots(selection_fixture, tmp_path):
    context, _ = selection_fixture
    base, pack = tmp_path / 'base-v2', tmp_path / 'pack-v2'
    base.mkdir()
    pack.mkdir()
    entry = make_graph(base)
    entry.write_text(entry.read_text() + '\n## Notes\n\n[Documentation](https://docs.example.test)\n[Flow](#flow)\n')
    (pack / 'input.json').write_text('{"type":"object"}')
    node = base / 'workflows/inspect.md.html'
    node.write_text(node.read_text().replace('../schemas/input.json', '<resource_pack>/input.json'))
    selected = replace(context, base_root=base, pack_root=pack, workflow_entry=entry)
    cache = DefinitionCache()

    first = cache(selected)
    second = cache(selected)

    assert second is first
    assert pack / 'input.json' in second.contents
    assert any(not isinstance(path, type(entry)) for path in second.contents)


@pytest.mark.parametrize('name', ['primary_graph.md.html', 'node.md.html', 'native.py', 'primary.toml'])
def test_changed_source_bytes_reload_even_with_restored_timestamp(selection_fixture, name):
    context, _ = selection_fixture
    cache = DefinitionCache()
    first = cache(context)
    path = context.base_root / name
    before = path.stat()
    path.write_bytes(path.read_bytes() + b'\n')
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    changed = cache(context)

    assert changed is not first
    assert changed.contents[path] == path.read_bytes()


def test_missing_resource_rejects_and_repair_reloads(selection_fixture, counted_loads):
    context, _ = selection_fixture
    cache = DefinitionCache()
    first = cache(context)
    path = context.base_root / 'native.py'
    body = path.read_bytes()
    path.unlink()
    with pytest.raises(ValueError, match='resource'):
        cache(context)
    with pytest.raises(ValueError, match='resource'):
        cache(context)
    path.write_bytes(body)

    repaired = cache(context)

    assert repaired is not first
    assert len(counted_loads) == 4


@pytest.mark.parametrize('outside', [False, True])
def test_retargeted_lexical_link_never_reuses_old_definition(selection_fixture, tmp_path, outside):
    context, _ = selection_fixture
    node = context.base_root / 'node.md.html'
    first_target = node.with_name('first.md.html')
    node.rename(first_target)
    node.symlink_to(first_target)
    cache = DefinitionCache()
    first = cache(context)
    directory = tmp_path.parent / (tmp_path.name + '-outside') if outside else tmp_path
    directory.mkdir(exist_ok=True)
    target = directory / 'second.md.html'
    target.write_bytes(first_target.read_bytes() + b'\n')
    node.unlink()
    node.symlink_to(target)

    if outside:
        with pytest.raises(ValueError, match='outside.*roots'):
            cache(context)
    else:
        changed = cache(context)
        assert changed is not first
        assert target in changed.contents
        assert first_target not in changed.contents


def test_installed_ownership_changes_are_not_hidden_by_reuse(selection_fixture):
    context, _ = selection_fixture
    cache = DefinitionCache()
    cache(context)
    restricted = replace(context, workflow_library_files=frozenset({'primary_graph.md.html'}))

    with pytest.raises(ValueError, match='not owned'):
        cache(restricted)


@pytest.mark.parametrize('version', [2, 3])
def test_session_reloads_retargeted_explicit_replacement(selection_fixture, version):
    context, schema = selection_fixture
    root = context.base_root
    entry = make_graph(root) if version == 2 else context.workflow_entry
    source = root / 'workflows/inspect.md.html' if version == 2 else root / 'node.md.html'
    first_target = source.with_name('first-node.md.html')
    second_target = source.with_name('second-node.md.html')
    first_target.write_bytes(source.read_bytes())
    second_target.write_bytes(source.read_bytes() + b'\n')
    alias = root / 'replacement.md.html'
    alias.symlink_to(first_target)
    selected = replace(context, workflow_entry=entry,
                       workflow_replacements=(('inspect', Path('replacement.md.html'), 'base'),))
    session = ConfigSession(selected, schema, environ={})
    first = session.result().tool_plan.definitions[0]
    alias.unlink()
    alias.symlink_to(second_target)

    changed = session.result().tool_plan.definitions[0]

    assert changed.definition_id != first.definition_id
    assert second_target in changed.contents
    assert first_target not in changed.contents


@pytest.mark.parametrize('limits', [{'max_entries': 1}, {'max_bytes': 1}, {'max_entries': 0}])
def test_cache_budgets_evict_or_bypass_without_affecting_loading(selection_fixture, counted_loads, limits):
    context, _ = selection_fixture
    cache = DefinitionCache(**limits)
    first = cache(context)
    other = replace(context, workflow_entry=context.base_root / 'dual_graph.md.html')
    cache(other)

    again = cache(context)

    assert again is not first
    assert len(counted_loads) == 3


def test_unresolvable_presentation_link_bypasses_cache(selection_fixture, counted_loads, monkeypatch):
    context, _ = selection_fixture
    cache = DefinitionCache()
    def unavailable(definition, entry):
        raise ValueError('unresolvable optional path')
    monkeypatch.setattr(cache, '_links', unavailable)

    first = cache(context)
    second = cache(context)

    assert first.definition_id == second.definition_id
    assert len(counted_loads) == 2


def test_link_watch_budget_bypasses_reuse_without_changing_admission(selection_fixture, counted_loads, monkeypatch):
    context, _ = selection_fixture
    cache = DefinitionCache()
    monkeypatch.setattr('api_agent.tooling.discovery_cache.MAX_WATCHED_LINKS', 1)

    first = cache(context)
    second = cache(context)

    assert first.definition_id == second.definition_id
    assert len(counted_loads) == 2


def test_session_reuses_parsing_but_recomposes_selection_and_inputs(selection_fixture, counted_loads):
    context, schema = selection_fixture
    session = ConfigSession(context, schema, environ={})
    session.select_operation(('base::tests/test_flow.py::test_dual',))
    baseline_loads = len(counted_loads)
    session.result(_review=True)
    session.result()
    assert len(counted_loads) == baseline_loads
    session.environ['SECONDARY_IMPLEMENTATION'] = 'rest.toml'
    session.environ['API_2_ENDPOINT'] = 'https://api.example.test'

    changed = session.result()

    assert changed.values['API_2_ENDPOINT'] == 'https://api.example.test'
    assert changed.values['API_2_PATH'] == '/records'
    assert 'API_2_HEADERS' not in session.schema.fields
    session.select_operation(('base::tests/test_flow.py::test_primary',))
    assert 'API_2_ENDPOINT' not in session.schema.fields
    assert len(counted_loads) == baseline_loads


def test_changed_catalog_adds_graph_and_changed_implementation_updates_default(selection_fixture):
    context, schema = selection_fixture
    session = ConfigSession(context, schema, environ={})
    session.select_operation(('base::tests/test_flow.py::test_dual',))
    implementation = context.base_root / 'graphql.toml'
    implementation.write_text(implementation.read_text().replace('default={}', 'default={"Accept"="application/json"}'))
    extra = graph(context.base_root, 'extra_graph', [('secondary', 'secondary.toml')])
    catalog = context.tool_catalog_path
    catalog.write_text(catalog.read_text() + '\n[graphs.extra]\nroot="base"\nentry="' + extra.name + '"\n')

    result = session.result()

    assert result.values['API_2_HEADERS'] == {'Accept': 'application/json'}
    assert 'extra' in session.tool_plan.catalog.definitions


@pytest.mark.asyncio
async def test_review_refresh_resolves_each_view_once_and_sees_external_edits(selection_fixture, monkeypatch):
    from api_agent.tui.app import ApiAgentApp
    from textual.widgets import DataTable
    from test_tool_config_session import session_for
    session = session_for(selection_fixture)
    app = ApiAgentApp(session.context, session.schema, session=session)
    async with app.run_test(size=(110, 35)) as pilot:
        app.review_names.update(session.schema.fields)
        app.show_configuration()
        await pilot.pause()
        calls = []
        original = session.result
        def resolve(**kwargs):
            calls.append(kwargs.get('_review', False))
            return original(**kwargs)
        monkeypatch.setattr(session, 'result', resolve)
        session.dotenv_path.write_text('SHARED=updated\n')

        app.refresh_configuration_rows(app.screen)

        assert calls == [False, True]
        table = app.screen.query_one(DataTable)
        shared = next(table.get_row_at(i) for i, row in enumerate(table.ordered_rows)
                      if row.key.value == 'SHARED')
        assert shared[1] == 'updated'
