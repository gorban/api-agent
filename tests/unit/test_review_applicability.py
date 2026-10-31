"""Review spans operations while respecting explicit configuration controllers."""
import asyncio
from pathlib import Path
import tomllib

import pytest
from textual.widgets import DataTable

from test_tui import fixture


@pytest.mark.parametrize('saved', [False, True], ids=['fresh', 'saved-jaeger'])
def test_trace_mode_review_transitions_and_saved_cleanup(tmp_path, saved):
    source = tomllib.loads((Path(__file__).resolve().parents[2] / 'cfg/config.toml').read_text())
    names = ('TRACE_MODE', 'TRACE_TARGET', 'JAEGER_VIEWER_PORT', 'TRACE_VIEWER_URL',
             'LANGFUSE_ADMIN_EMAIL', 'LANGFUSE_ADMIN_PASSWORD', 'LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY')
    fields = {name: source['fields'][name] for name in names}
    fields['COLLECT_ONLY'] = {'type': 'bool', 'default': False, 'acknowledge_default': False}
    if saved:
        (tmp_path / '.env').write_text('JAEGER_VIEWER_PORT=16687\n')
    app = fixture(tmp_path, fields=fields)

    async def scenario():
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            for mode in ('', 'OTEL', 'LangFuse', ''):
                app.session.set_value('TRACE_MODE', mode)
                app.review_names.update(app.schema.fields)
                app.show_configuration()
                await pilot.pause()
                rows = {row[0]: row for row in (app.screen.query_one(DataTable).get_row_at(i)
                        for i in range(app.screen.query_one(DataTable).row_count))}
                pending = app.configuration_pending()
                assert ('JAEGER_VIEWER_PORT' in rows) is (mode == 'OTEL')
                assert ('TRACE_VIEWER_URL' in rows) is (mode == 'OTEL')
                assert ('JAEGER_VIEWER_PORT' in pending) is (mode == 'OTEL' and not saved)
                for name in names[4:]:
                    assert (name in rows) is (mode == 'LangFuse')
                if mode == 'OTEL':
                    assert str(rows['JAEGER_VIEWER_PORT'][1]) == ('16687' if saved else '16686')
                if mode == 'LangFuse':
                    assert rows['LANGFUSE_ADMIN_EMAIL'][1] == 'admin@local.dev'
            app.show_cleanup()
            await pilot.pause()
            rows = [app.screen.query_one(DataTable).get_row_at(i)
                    for i in range(app.screen.query_one(DataTable).row_count)]
            assert [row[1] for row in rows] == (['JAEGER_VIEWER_PORT'] if saved else [])
            if saved:
                assert (tmp_path / '.env').read_text() == 'JAEGER_VIEWER_PORT=16687\n'
            else:
                assert not (tmp_path / '.env').exists()

    asyncio.run(scenario())


@pytest.mark.parametrize('predicate, values, expected', [
    ({}, {}, True),
    ({'TUI_MODE': 'run-local', 'COLLECT_ONLY': False, 'operation': 'ui_session'}, {}, True),
    ({'TRACE_MODE': 'OTEL'}, {'TRACE_MODE': ''}, False),
    ({'TRACE_MODE': ['OTEL', 'LangFuse']}, {'TRACE_MODE': 'OTEL'}, True),
    ({'any': [{'TUI_MODE': 'run-local', 'TRACE_MODE': 'OTEL'},
              {'TUI_MODE': 'integration', 'TRACE_MODE': 'LangFuse'}]}, {'TRACE_MODE': 'LangFuse'}, True),
    ({'any': [{'TRACE_MODE': 'OTEL'}, {'TRACE_MODE': 'LangFuse'}]}, {'TRACE_MODE': ''}, False),
    ({'ENABLED': True, 'any': [{'TUI_MODE': 'unit'}, {'TRACE_MODE': 'OTEL'}]}, {'ENABLED': True}, True),
], ids=['unconditional', 'operation-controls', 'inactive-mode', 'mode-list', 'mixed-any', 'inactive-any', 'operation-branch'])
def test_review_predicate_retains_controller_conditions(predicate, values, expected):
    from api_agent.config.session import _review_predicate
    from api_agent.config.resolve import _matches
    assert _matches(_review_predicate(predicate), values) is expected


def test_runtime_required_recovery_row_remains_available(tmp_path):
    app = fixture(tmp_path, fields={
        'ENABLED': {'type': 'bool', 'default': False},
        'DETAIL': {'type': 'str', 'acknowledge_default': False,
                   'review_optional': True, 'applies_when': {'ENABLED': True}},
    })

    async def scenario():
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            assert 'DETAIL' not in app.configuration_pending()
            assert app.session.require_fields(('DETAIL',), snapshot=app.session.result().values)
            app.show_configuration()
            await pilot.pause()
            rows = [app.screen.query_one(DataTable).get_row_at(i)
                    for i in range(app.screen.query_one(DataTable).row_count)]
            assert rows[0][0] == 'DETAIL'
            assert str(rows[0][1]) == 'Required'
            assert 'DETAIL' in app.configuration_pending()
            app.session.set_value('ENABLED', 'True')
            app.session.set_value('DETAIL', 'ready', persist=True)
            review = app.session.result(_review=True)
            execution = app.session.result()
            assert review.values['DETAIL'] == execution.values['DETAIL'] == 'ready'
            assert 'DETAIL' not in review.missing
            assert 'DETAIL' not in execution.missing
            assert 'DETAIL' not in app.configuration_pending()

    asyncio.run(scenario())
