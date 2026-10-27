"""Late background reads cannot replace newer user navigation or configuration."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.tui.app import ApiAgentApp
from test_tui import fixture


@pytest.mark.parametrize('change', ['screen', 'session', 'selection', 'tests', 'mode', 'controls',
                                   'generation', 'failure', 'stale_failure', 'sources', 'source_failure', 'current'])
def test_selected_review_delivery_requires_current_owner(tmp_path, change):
    async def scenario():
        app = fixture(tmp_path)
        screen = SimpleNamespace(query_one=Mock())
        selection = SimpleNamespace(selected_tests=())
        state = SimpleNamespace(screen=screen, session=app.session, selection=selection,
                                mode='integration', show=Mock())
        review = SimpleNamespace(rows=[('case', ('case', 'setting', 'value', 'source'))],
                                 sources_current=Mock(return_value=True))
        async def background(label, function, *args):
            if change == 'screen':
                state.screen = object()
            elif change == 'session':
                state.session = object()
            elif change == 'selection':
                state.selection = object()
            elif change == 'tests':
                selection.selected_tests = ('new case',)
            elif change == 'mode':
                state.mode = 'unit'
            elif change == 'controls':
                state.session._choices['COUNT'] = '9'
            elif change == 'generation':
                state._case_review_generation += 1
            elif change == 'sources':
                review.sources_current.return_value = False
            elif change == 'source_failure':
                review.sources_current.side_effect = OSError('private source details')
            elif change in {'failure', 'stale_failure'}:
                if change == 'stale_failure':
                    state.screen = object()
                raise OSError('private read details')
            return review
        state.background_reads = SimpleNamespace(run=background)
        await ApiAgentApp.show_selected_test_configuration(state)
        if change == 'current':
            state.show.assert_called_once()
            assert state.show.call_args.kwargs['rows'] == review.rows
        else:
            state.show.assert_not_called()
        if change in {'failure', 'sources', 'source_failure'}:
            screen.query_one.return_value.update.assert_called_once()
            assert 'private' not in str(screen.query_one.return_value.update.call_args)
        else:
            screen.query_one.assert_not_called()
    asyncio.run(scenario())


def test_selected_review_resolution_runs_in_background_fork(tmp_path, monkeypatch):
    import threading
    app = fixture(tmp_path)
    selection = SimpleNamespace(selected_tests=())
    state = SimpleNamespace(screen=SimpleNamespace(query_one=Mock()), session=app.session,
        selection=selection, mode='integration', show=Mock(), background_reads=app.background_reads)
    ui_thread = threading.get_ident()
    original = type(app.session).review_snapshot
    readers = []
    def snapshot(session):
        assert session is not app.session
        assert threading.get_ident() != ui_thread
        readers.append(session)
        return original(session)
    monkeypatch.setattr(type(app.session), 'review_snapshot', snapshot)
    asyncio.run(ApiAgentApp.show_selected_test_configuration(state))
    assert len(readers) == 1
    state.show.assert_called_once()
