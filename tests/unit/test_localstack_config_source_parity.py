"""Real demo storage defaults agree across explicit configuration sources."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import DataTable, Select

from api_agent.config import compose_schema, load_declarations, load_shared_schema
from api_agent.config.dotenv import read_dotenv
from api_agent.config.session import ConfigSession
from api_agent.tui.app import ApiAgentApp


@pytest.fixture
def demo_configuration(tmp_path):
    root = Path(__file__).resolve().parents[2]
    shared = load_shared_schema(root)
    schema = compose_schema(shared, load_declarations(root / 'demo/cfg/config.toml',
        base_schema=shared, source='pack_default'))
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path / 'demo', owner_root=tmp_path,
        pack_id='demo', external_pack=False, dotenv_path=tmp_path / '.env',
        anchors={name: tmp_path for name in ('base_root', 'pack_root', 'owner_root')})
    return context, schema


@pytest.mark.parametrize('source', ['dotenv', 'environment', 'cli', 'interactive-save'])
def test_localstack_true_derives_storage_without_missing_bucket(demo_configuration, source):
    context, schema = demo_configuration
    argv, environ = [], {}
    if source == 'dotenv':
        context.dotenv_path.write_text('ALLURE_S3_USE_LOCALSTACK=True\n')
    elif source == 'environment':
        environ['ALLURE_S3_USE_LOCALSTACK'] = 'True'
    elif source == 'cli':
        argv.append('--allure-s3-use-localstack=True')
    session = ConfigSession(context, schema, argv=argv, environ=environ)
    if source == 'interactive-save':
        session.set_value('ALLURE_S3_USE_LOCALSTACK', 'True', persist=True)
    session.set_value('TUI_MODE', 'integration')

    result = session.result()

    assert not result.errors
    assert 'ALLURE_S3_BUCKET' not in result.missing
    assert result.values['ALLURE_S3_BUCKET'] == 'allure-results'
    assert result.values['ALLURE_S3_ENDPOINT'] == 'http://localhost:4566'
    assert not result.fields['ALLURE_S3_BUCKET'].explicit
    assert {'ALLURE_S3_BUCKET', 'ALLURE_S3_ENDPOINT'} <= {
        item.name for item in session.default_candidates()}
    session.save_defaults(['ALLURE_S3_BUCKET', 'ALLURE_S3_ENDPOINT'])
    restarted = ConfigSession(context, schema, argv=argv, environ=environ)
    restarted.set_value('TUI_MODE', 'integration')
    assert restarted.result().fields['ALLURE_S3_BUCKET'].explicit
    assert restarted.result().values['ALLURE_S3_ENDPOINT'] == 'http://localhost:4566'
    assert not {'ALLURE_S3_BUCKET', 'ALLURE_S3_ENDPOINT'} & {
        item.name for item in restarted.default_candidates()}


def test_review_edit_localstack_and_acknowledge_derived_defaults(demo_configuration):
    context, schema = demo_configuration
    from unittest.mock import Mock
    from api_agent.testing import TestIdentity as Identity
    collector = Mock(return_value=[Identity('base', context.base_root, 'test_sample.py', 'test_sample', 'integration')])
    app = ApiAgentApp(context, schema, environ={}, collector=collector,
        container_runtime=SimpleNamespace(inventory=lambda: ()))

    def focus(name):
        table = app.screen.query_one(DataTable)
        table.move_cursor(row=next(i for i, row in enumerate(table.ordered_rows)
                                  if row.key.value == name))

    async def scenario():
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            app.show_configuration()
            await pilot.pause()
            focus('ALLURE_S3_USE_LOCALSTACK')
            await pilot.pause()
            await pilot.press('e')
            await pilot.pause()
            app.screen.query_one(Select).value = 'True'
            await pilot.click('#submit')
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            app.session.set_value('TUI_MODE', 'integration')
            assert 'ALLURE_S3_BUCKET' not in app.session.result().missing
            await pilot.press('c')
            await pilot.pause()
            await app.choose('mode-integration')
            await pilot.pause()
            collector.assert_called_once()
            assert app.screen.kind == 'tests'
            assert 'ALLURE_S3_BUCKET' not in read_dotenv(context.dotenv_path)
            assert not app.session.result().fields['ALLURE_S3_BUCKET'].explicit
            app.show_configuration()
            await pilot.pause()
            for name in ('ALLURE_S3_BUCKET', 'ALLURE_S3_ENDPOINT'):
                assert name in app.configuration_pending()
                focus(name)
                await pilot.pause()
                await pilot.press('a')
                await pilot.pause()
                assert name not in app.configuration_pending()
            saved = read_dotenv(context.dotenv_path)
            assert saved['ALLURE_S3_USE_LOCALSTACK'] == 'True'
            assert saved['ALLURE_S3_BUCKET'] == 'allure-results'
            assert saved['ALLURE_S3_ENDPOINT'] == 'http://localhost:4566'
    asyncio.run(scenario())


def test_all_mode_review_inventory_does_not_make_inactive_bucket_required(demo_configuration):
    context, schema = demo_configuration
    session = ConfigSession(context, schema, environ={})
    session.set_value('TUI_MODE', 'unit')

    assert 'ALLURE_S3_BUCKET' not in session.result(_review=True).missing
    assert 'ALLURE_S3_BUCKET' not in session.result().missing
    assert 'ALLURE_S3_BUCKET' not in session.result().values


@pytest.mark.parametrize('mode', ['integration', 'e2e'])
def test_review_bucket_requirement_tracks_active_reporting_mode(demo_configuration, mode):
    context, schema = demo_configuration
    session = ConfigSession(context, schema, environ={'TUI_MODE': mode})

    assert 'ALLURE_S3_BUCKET' in session.result(_review=True).missing
    assert 'ALLURE_S3_BUCKET' in session.result().missing

    session.set_value('SKIP_ALLURE_REPORTING', 'True')

    assert 'ALLURE_S3_BUCKET' not in session.result(_review=True).missing
    assert 'ALLURE_S3_BUCKET' not in session.result().missing
