"""The live picker remains usable when selected cases cannot share a tool plan."""
import threading
from unittest.mock import Mock

import pytest
from textual.widgets import DataTable, Static

from api_agent.testing.collection import TestIdentity
from api_agent.tui.app import ApiAgentApp
from test_tool_config_session import PRIMARY, session_for
from test_tool_selection import graph, selection_fixture

OTHER = 'base::tests/test_other.py::test_other'


@pytest.fixture
def conflicting_app(selection_fixture):
    context, _ = selection_fixture
    implementation = context.base_root / 'other-implementation.toml'
    implementation.write_text((context.base_root / 'graphql.toml').read_text()
                              .replace('[inputs.ENDPOINT]\ntype="str"', '[inputs.ENDPOINT]\ntype="int"'))
    secondary = context.base_root / 'secondary.toml'
    secondary.write_text(secondary.read_text().replace('API_2_ENDPOINT', 'API_ENDPOINT')
                         .replace('implementation="graphql.toml"', 'implementation="other-implementation.toml"'))
    graph(context.base_root, 'other_graph', [('secondary', 'secondary.toml')])
    context.tool_catalog_path.write_text(context.tool_catalog_path.read_text()
        + '\n[graphs.other]\nroot="base"\nentry="other_graph.md.html"\n'
        + '[test_cases."' + OTHER + '"]\ngraphs=["other"]\n')
    identities = tuple(TestIdentity(identity, 'base', identity.split('::')[1],
                                    identity.split('::', 1)[1]) for identity in (PRIMARY, OTHER))
    session = session_for(selection_fixture, argv=('--tui-mode=integration',))
    runner = Mock(side_effect=AssertionError('invalid selection started execution'))
    app = ApiAgentApp(context, session.schema, session=session,
                      collector=lambda *args: identities, integration_runner=runner)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize('toggle', ['row', 'bulk', 'search'])
async def test_conflicting_default_union_stays_in_picker_and_recovers(conflicting_app, toggle):
    app = conflicting_app
    runner = app.integration_runner
    session = app.session
    async with app.run_test(size=(110, 35)) as pilot:
        app.mode = 'integration'
        app.collection_cancel = threading.Event()
        await app._collect_tests(app.collection_cancel)
        await pilot.pause()

        assert app.screen.kind == 'tests'
        assert app.selection.selected_ids == {PRIMARY, OTHER}
        assert 'Conflicting configuration binding ownership' in str(app.screen.query_one('#error', Static).render())
        assert app.screen.query_one(DataTable).row_count >= 2

        await pilot.press('s')
        await pilot.pause()
        assert app.screen.kind == 'tests'
        assert 'Conflicting configuration binding ownership' in str(app.screen.query_one('#error', Static).render())
        assert not app.test_run_active()
        runner.assert_not_called()

        if toggle == 'row':
            app.toggle_row(OTHER)
        elif toggle == 'bulk':
            app.screen.toggle_test_rows((OTHER,))
        else:
            from api_agent.tui.search import SearchInput
            await pilot.press('slash')
            app.screen.query_one(SearchInput).value = 'test_other'
            await pilot.pause()
            await pilot.press('enter', 'escape')
        await pilot.pause()
        assert app.selection.selected_ids == {PRIMARY}
        assert not str(app.screen.query_one('#error', Static).render()).strip()
        assert session.tool_plan.selection.test_ids == (PRIMARY,)
        app.toggle_row(OTHER)
        await pilot.pause()
        assert 'Conflicting configuration binding ownership' in str(app.screen.query_one('#error', Static).render())
        app.toggle_row(OTHER)
        await pilot.pause()
        app.show_selection()
        await pilot.pause()
        await app.choose('review-config')
        await pilot.pause()
        assert app.screen.kind == 'case-configuration'
        assert not app.test_run_active()
        runner.assert_not_called()


@pytest.mark.asyncio
async def test_search_restores_union_error_after_query_validation(conflicting_app):
    from api_agent.tui.search import SearchInput
    app = conflicting_app
    async with app.run_test(size=(110, 35)) as pilot:
        app.mode = 'integration'
        app.collection_cancel = threading.Event()
        await app._collect_tests(app.collection_cancel)
        await pilot.pause()
        await pilot.press('slash')
        assert 'Conflicting configuration binding ownership' in str(app.screen.query_one('#error', Static).render())
        control = app.screen.query_one(SearchInput)
        control.value = '['
        await pilot.pause()
        query_error = str(app.screen.query_one('#error', Static).render())
        assert query_error.strip() and 'Conflicting configuration binding ownership' not in query_error
        control.value = 'test_other'
        await pilot.pause()
        assert 'Conflicting configuration binding ownership' in str(app.screen.query_one('#error', Static).render())
        await pilot.press('enter')
        assert app.selection.selected_ids == {PRIMARY}
        assert not str(app.screen.query_one('#error', Static).render()).strip()
        await pilot.press('enter')
        assert 'Conflicting configuration binding ownership' in str(app.screen.query_one('#error', Static).render())
        await pilot.press('escape')
        assert 'Conflicting configuration binding ownership' in str(app.screen.query_one('#error', Static).render())
        assert app.selection.selected_ids == {PRIMARY, OTHER}
        assert not app.test_run_active()
        app.integration_runner.assert_not_called()


@pytest.mark.asyncio
async def test_picker_retirement_has_no_delayed_error_update(conflicting_app):
    app = conflicting_app
    async with app.run_test(size=(110, 35)) as pilot:
        app.mode = 'integration'
        app.collection_cancel = threading.Event()
        await app._collect_tests(app.collection_cancel)
        app.show_menu()
        await pilot.pause()
        assert app.screen.kind == 'menu'
        assert not app.test_run_active()
        app.integration_runner.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_automatic_start_leaves_editable_picker(conflicting_app):
    app = conflicting_app
    async with app.run_test(size=(110, 35)) as pilot:
        app.mode = 'integration'
        app.auto_start_pending = True
        app.collection_cancel = threading.Event()
        await app._collect_tests(app.collection_cancel)
        await pilot.pause()
        assert app.screen.kind == 'tests'
        assert 'Conflicting configuration binding ownership' in str(app.screen.query_one('#error', Static).render())
        assert not app.auto_start_pending
        app.toggle_row(OTHER)
        await pilot.pause()
        assert app.selection.selected_ids == {PRIMARY}
        assert not str(app.screen.query_one('#error', Static).render()).strip()
        assert not app.test_run_active()
        app.integration_runner.assert_not_called()
