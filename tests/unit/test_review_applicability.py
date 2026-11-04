"""Review spans operations while respecting explicit configuration controllers."""
import asyncio
from pathlib import Path
import tomllib

import pytest
from textual.widgets import DataTable

from test_tui import fixture


@pytest.mark.parametrize('provider, auth_mode, visible', [
    ('gpt', 'subscription', True),
    ('gpt', 'api_key', False),
    ('bedrock', 'session', False),
])
def test_subscription_allocation_fields_belong_to_selected_provider(tmp_path, provider, auth_mode, visible):
    from api_agent.config import load_shared_schema, resolve_config
    from api_agent.providers.loading import bootstrap_schema

    root = Path(__file__).resolve().parents[2]
    names = {'LLM_QUOTA_PERIOD', 'LLM_PAY_PERIOD', 'LLM_PAY_PERIOD_LIMIT'}
    shared = load_shared_schema()
    assert not names.intersection(shared.fields)
    settings = {'LLM_PROVIDER': str(root / 'src/api_agent/llm_providers' / provider / 'factory.py') + ':factory',
                ('GPT_AUTH_MODE' if provider == 'gpt' else 'BEDROCK_AUTH_MODE'): auth_mode}
    schema = bootstrap_schema(shared, (), settings, {})
    result = resolve_config(schema, environ=settings)
    assert names.intersection(result.values) == (names if visible else set())
    assert names.intersection(schema.fields) == (names if provider == 'gpt' else set())

    app = fixture(tmp_path)
    app.schema = shared
    app.session.schema = shared
    app.session.environ.update(settings)
    app.context.anchors = {name: tmp_path for name in ('owner_root', 'base_root', 'pack_root')}

    async def scenario():
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            app.review_names.update(names)
            app.show_configuration()
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            rows = {table.get_row_at(i)[0] for i in range(table.row_count)}
            assert names.intersection(rows) == (names if visible else set())
            assert names.intersection(app.configuration_pending()) == (names if visible else set())

    asyncio.run(scenario())


@pytest.mark.parametrize('saved', [False, True], ids=['fresh', 'saved-path'])
def test_csv_path_review_requires_enabled_toggle(tmp_path, saved):
    source = tomllib.loads((Path(__file__).resolve().parents[2] / 'cfg/config.toml').read_text())
    names = ('TOKEN_USAGE_CSV_ENABLED', 'TOKEN_USAGE_CSV', 'COLLECT_ONLY')
    app = fixture(tmp_path, fields={name: source['fields'][name] for name in names})
    app.context.anchors['owner_root'] = tmp_path
    if saved:
        (tmp_path / '.env').write_text('TOKEN_USAGE_CSV=custom.csv\n')

    async def scenario():
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            for enabled in (None, 'True', 'False', 'True'):
                if enabled is not None:
                    app.session.set_value('TOKEN_USAGE_CSV_ENABLED', enabled)
                app.review_names.update(app.schema.fields)
                app.show_configuration()
                await pilot.pause()
                table = app.screen.query_one(DataTable)
                rows = {table.get_row_at(i)[0] for i in range(table.row_count)}
                assert ('TOKEN_USAGE_CSV' in rows) is (enabled == 'True')
                assert ('TOKEN_USAGE_CSV' in app.configuration_pending()) is (enabled == 'True' and not saved)
                if enabled == 'True':
                    assert app.session.result(_review=True).values['TOKEN_USAGE_CSV'] == tmp_path / (
                        'custom.csv' if saved else 'token_usage.csv')
            if saved:
                assert (tmp_path / '.env').read_text() == 'TOKEN_USAGE_CSV=custom.csv\n'

    asyncio.run(scenario())


@pytest.mark.parametrize('saved', [False, True], ids=['fresh', 'saved-jaeger'])
def test_trace_mode_review_transitions_and_saved_cleanup(tmp_path, saved):
    source = tomllib.loads((Path(__file__).resolve().parents[2] / 'src/api_agent/telemetry/trace_adapters/cfg/config.toml').read_text())
    names = ('TRACE_MODE', 'TRACE_EXPORT_ENDPOINT', 'JAEGER_VIEWER_PORT', 'TRACE_VIEWER_URL',
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
                assert ('TRACE_VIEWER_URL' in rows) is bool(mode)
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
