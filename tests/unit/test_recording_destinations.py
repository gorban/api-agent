"""Disabled destinations remain explicit configuration, never the current directory."""
import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from textual.widgets import DataTable, Input

from api_agent.config import (compose_schema, load_declarations, load_shared_schema,
                                 resolve_config, schema_from_dict)
from api_agent.runtime.resource_pack import load_launch_context
from api_agent.testing import recorded
from api_agent.testing.storage import RunStorage
from test_recorded_integration import fixture as recording_fixture, fake_runner, Storage
from test_recording_storage import FakeS3
from test_tui import fixture as tui_fixture


@pytest.mark.parametrize('external', [False, True])
def test_toggle_defaults_csv_to_operation_owner_not_launcher_or_cwd(tmp_path, monkeypatch, external):
    base = Path(__file__).resolve().parents[2]
    from test_identity_extensions import pack as make_resource_pack
    pack = make_resource_pack(tmp_path, identity=False).pack_root if external else None
    context = load_launch_context(base, pack, launcher_root=base)
    shared = load_shared_schema(base)
    schema = compose_schema(shared, load_declarations(context.config_path, base_schema=shared), context.defaults)
    monkeypatch.chdir(tmp_path)
    settings = {'TUI_MODE': 'integration', 'ALLURE_S3_BUCKET': 'synthetic'}
    disabled = resolve_config(schema, environ=settings, anchors=context.anchors, pack_launcher=external)
    assert not disabled.errors
    assert disabled.values['TOKEN_USAGE_CSV_ENABLED'] is False
    assert 'TOKEN_USAGE_CSV' not in disabled.values
    enabled = resolve_config(schema, argv=['--token-usage-csv-enabled'], environ=settings,
                             anchors=context.anchors, pack_launcher=external)
    assert not enabled.errors
    assert enabled.values['TOKEN_USAGE_CSV'] == context.owner_root / 'token_usage.csv'
    assert enabled.fields['TOKEN_USAGE_CSV'].source == 'shared_default'
    override = resolve_config(schema, argv=['--token-usage-csv-enabled', '--token-usage-csv=custom.csv'],
                              environ=settings, anchors=context.anchors, pack_launcher=external)
    assert override.values['TOKEN_USAGE_CSV'] == context.owner_root / 'custom.csv'
    assert override.fields['TOKEN_USAGE_CSV'].explicit


@pytest.mark.parametrize('name', ['TOKEN_USAGE_CSV', 'ALLURE_RESULTS_DIR', 'ALLURE_SECONDARY_DIR'])
@pytest.mark.parametrize('source', ['dotenv', 'environment', 'cli'])
def test_empty_paths_are_explicit_disabled_values_not_root_paths(name, source, tmp_path):
    options = {'TUI_MODE': 'integration', 'ALLURE_S3_BUCKET': 'synthetic',
               'TOKEN_USAGE_CSV_ENABLED': 'True'}
    kwargs = {'environ': options, 'anchors': {'owner_root': tmp_path}}
    if source == 'cli':
        kwargs['argv'] = [f'--{name.lower().replace("_", "-")}=']
    elif source == 'dotenv':
        kwargs['dotenv'] = {name: ''}
    else:
        options[name] = ''
    result = resolve_config(load_shared_schema(), **kwargs)
    assert not result.errors
    assert result.values[name] == ''
    assert result.fields[name].explicit and result.fields[name].source == source


def test_empty_path_support_is_opt_in_and_does_not_bypass_enum_validation(tmp_path):
    def schema(**metadata):
        return schema_from_dict({'version': 1, 'fields': {'P': {
            'type': 'path', 'anchor': 'owner_root', **metadata}}})
    assert 'nonempty path' in resolve_config(schema(), environ={'P': ''}).errors[0]
    permitted = resolve_config(schema(allow_empty=True), environ={'P': '  '})
    assert not permitted.errors and permitted.values['P'] == ''
    assert 'expected one of' in resolve_config(schema(allow_empty=True, enum=['allowed']),
                                             environ={'P': ''}).errors[0]
    for metadata, message in [({'type': 'path', 'anchor': 'owner_root', 'allow_empty': 'true'},
                               'allow_empty must be boolean'),
                              ({'type': 'str', 'allow_empty': True}, 'allow_empty requires path or int type')]:
        with pytest.raises(ValueError, match=message):
            schema_from_dict({'version': 1, 'fields': {'P': metadata}})


@pytest.mark.parametrize('enabled', [False, None])
def test_disabled_toggle_does_not_append_even_with_explicit_csv_path(tmp_path, enabled):
    context, test, config = recording_fixture(tmp_path)
    target = tmp_path / 'existing.csv'
    target.write_bytes(b'unchanged existing data\n')
    config['TOKEN_USAGE_CSV'] = target
    if enabled is not None:
        config['TOKEN_USAGE_CSV_ENABLED'] = enabled
    result = recorded.run_recorded_integration(context, [test], config, accepted=True,
                                              storage=Storage(), runner=fake_runner())
    assert result.persisted and result.exit_code == 0
    assert target.read_bytes() == b'unchanged existing data\n'
    assert (result.directory / 'token-usage.csv').is_file()


@pytest.mark.parametrize('status', ['passed', 'failed', 'cancelled'])
@pytest.mark.parametrize('storage_failure', [False, True])
def test_empty_local_destinations_keep_s3_and_cleanup_staging(tmp_path, monkeypatch, status, storage_failure):
    context, test, config = recording_fixture(tmp_path)
    config.update(ALLURE_RESULTS_DIR='', ALLURE_SECONDARY_DIR='',
                  TOKEN_USAGE_CSV_ENABLED=True, TOKEN_USAGE_CSV='')
    monkeypatch.chdir(tmp_path)
    staging = []
    original = recorded.tempfile.TemporaryDirectory
    def temporary(*args, **kwargs):
        value = original(*args, **kwargs)
        if kwargs.get('prefix') == 'api-agent-allure-':
            staging.append(Path(value.name))
        return value
    monkeypatch.setattr(recorded.tempfile, 'TemporaryDirectory', temporary)
    s3 = FakeS3()
    s3.fail = storage_failure
    storage = RunStorage('synthetic', client=s3, secondary_directory='')
    assert storage.secondary_directory is None
    result = recorded.run_recorded_integration(context, [test], config, accepted=True,
                                              storage=storage, runner=fake_runner(status))
    assert result.exit_code == (130 if status == 'cancelled' else int(status != 'passed' or storage_failure))
    assert result.persisted is not storage_failure
    assert result.directory is None
    assert len(staging) == 1 and not staging[0].exists()
    assert not list(tmp_path.iterdir())
    if not storage_failure:
        names = {key.rsplit('/', 1)[-1] for key in s3.objects}
        assert {'token-usage.csv', 'usage-summary.json', 'manifest.json'} <= names


def test_temporary_staging_is_cleaned_when_runner_raises(tmp_path):
    context, test, config = recording_fixture(tmp_path)
    config['ALLURE_RESULTS_DIR'] = ''
    staged = []
    def abort(*args, **kwargs):
        raise RuntimeError('controlled abort')
    class CaptureRecorder(recorded.RunRecorder):
        def __init__(self, directory, *args, **kwargs):
            staged.append(directory)
            super().__init__(directory, *args, **kwargs)
    from unittest.mock import patch
    with patch.object(recorded, 'RunRecorder', CaptureRecorder):
        with pytest.raises(RuntimeError, match='controlled abort'):
            recorded.run_recorded_integration(context, [test], config, accepted=True,
                                              storage=Storage(), runner=abort)
    assert len(staged) == 1 and not staged[0].parents[1].exists()


def test_saved_empty_destinations_are_acknowledged_and_suppress_startup_review(tmp_path):
    async def scenario():
        shared = load_shared_schema()
        names = ('TOKEN_USAGE_CSV_ENABLED', 'TOKEN_USAGE_CSV', 'ALLURE_RESULTS_DIR', 'ALLURE_SECONDARY_DIR')
        app = tui_fixture(tmp_path)
        app.schema = replace(app.schema, fields={**app.schema.fields,
                            **{name: shared.fields[name] for name in names},
                            'COLLECT_ONLY': shared.fields['COLLECT_ONLY'],
                            'ALLURE_VIEW_SECONDARY': shared.fields['ALLURE_VIEW_SECONDARY']})
        app.session.schema = app.schema
        app.context.anchors['owner_root'] = tmp_path
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            app.session.set_value('TOKEN_USAGE_CSV_ENABLED', 'True', persist=True)
            app.show_configuration()
            await pilot.pause()
            assert app.session.result(_review=True).values['TOKEN_USAGE_CSV'] == tmp_path / 'token_usage.csv'
            for name in names[1:]:
                table = app.screen.query_one(DataTable)
                table.move_cursor(row=next(i for i in range(table.row_count) if table.get_row_at(i)[0] == name))
                await pilot.press('e')
                await pilot.pause()
                app.screen.query_one(Input).value = ''
                await pilot.press('enter')
                await pilot.pause()
                assert app.session.result(_review=True).values[name] == ''
                assert app.session.result(_review=True).fields[name].explicit
            app.session.set_value('ALLURE_VIEW_SECONDARY', 'False', persist=True)
            assert not app.configuration_pending()
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'menu' and not app.screen.query('#review-config')
        relaunched = tui_fixture(tmp_path)
        relaunched.schema = app.schema
        relaunched.session.schema = app.schema
        relaunched.context.anchors = app.context.anchors
        async with relaunched.run_test() as pilot:
            await pilot.pause()
            assert relaunched.screen.kind == 'menu'
            assert not relaunched.configuration_pending()
            for name in names[1:]:
                assert relaunched.session.result(_review=True).values[name] == ''
    asyncio.run(scenario())


@pytest.mark.parametrize('view_secondary', [False, True])
def test_secondary_copy_publication_is_independent_of_viewing_policy(tmp_path, monkeypatch, view_secondary):
    context, test, config = recording_fixture(tmp_path)
    secondary = tmp_path / 'copies'
    config.update(ALLURE_VIEW_SECONDARY=view_secondary, ALLURE_SECONDARY_DIR=secondary)
    s3 = FakeS3()
    constructor = recorded.RunStorage
    monkeypatch.setattr(recorded, 'RunStorage', lambda *args, **kwargs: constructor(*args, client=s3, **kwargs))

    result = recorded.run_recorded_integration(context, [test], config, accepted=True, runner=fake_runner())

    assert result.exit_code == 0 and result.persisted
    assert s3.objects
    copies = tuple(secondary.glob('*/*/manifest.json'))
    assert len(copies) == 1
    assert copies[0].read_bytes() == (result.directory / 'manifest.json').read_bytes()
