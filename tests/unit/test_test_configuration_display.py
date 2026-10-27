"""Selected case review shows inert effective controls with bounded display."""
from pathlib import Path

import pytest

from api_agent.providers.loading import bootstrap_schema
from api_agent.config import load_shared_schema, resolve_config
from api_agent.testing.collection import TestIdentity
from api_agent.tui.test_configuration import profile_rows

ROOT = Path(__file__).resolve().parents[2]


def configured(tmp_path, **values):
    provider = tmp_path / 'provider'
    (provider / 'cfg').mkdir(parents=True)
    (provider / 'factory.py').write_text("raise AssertionError('display imported provider')\n")
    (provider / 'cfg/config.toml').write_text('''version = 1
[fields.SYNTH_API_MODE]
type = "str"
default = "base"
enum = ["base", "private-value"]
[fields.PRIVATE_API_MODE]
type = "str"
default = "inherited-private"
sensitive = true
''')
    (provider / 'test-profiles.toml').write_text('''version = 1
factory = "factory"
provider_fields = ["SYNTH_API_MODE"]
[profiles.ordinary]
cases = ["tests/integration/test_one.py::test_one"]
[profiles.ordinary.settings]
SYNTH_API_MODE = "private-value"
LLM_DEFAULT_MODEL = "ordinary-model"
[profiles.workflow]
cases = ["tests/integration/test_two.py::test_two"]
[profiles.workflow.settings]
LLM_DEFAULT_MODEL = "workflow-model"
LLM_WORKFLOW_TEST_MODEL = "profile-model"
''')
    config = {'TUI_MODE': 'integration', 'LLM_PROVIDER': str(provider / 'factory.py') + ':factory',
              'LLM_DEFAULT_MODEL': 'application-model', **values}
    schema = bootstrap_schema(load_shared_schema(), ['--help'], {}, {}, config, owner_root=ROOT)
    result = resolve_config(schema, environ={}, dotenv={}, overrides=config)
    tests = tuple(TestIdentity(f'base:{i}', 'base', f'test_{i}.py',
        f'tests/integration/test_{name}.py::test_{name}', root=ROOT)
        for i, name in enumerate(('one', 'two')))
    return tests, result, schema


def test_effective_profiles_are_per_identity_literal_and_credential_safe(tmp_path):
    tests, result, schema = configured(tmp_path, LLM_WORKFLOW_TEST_MODEL='[explicit]')
    result.values['UNATTENDED_AUTH'] = 'private-auth-value'
    rows = [cells for _, cells in profile_rows(tests, result, schema, ROOT)]
    lookup = {(cells[0], cells[1]): cells[2:] for cells in rows}
    assert lookup[(tests[0].id, 'LLM_DEFAULT_MODEL')] == (
        'ordinary-model', 'provider_test_profile:test-profiles.toml:ordinary')
    assert lookup[(tests[1].id, 'LLM_DEFAULT_MODEL')][0] == 'workflow-model'
    assert lookup[(tests[1].id, 'LLM_WORKFLOW_TEST_MODEL')] == ('[explicit]', 'interactive')
    assert lookup[(tests[0].id, 'SYNTH_API_MODE')][0] == 'private-value'
    assert lookup[(tests[0].id, 'PRIVATE_API_MODE')][0] == 'Configured'
    assert 'inherited-private' not in repr(rows) and 'private-auth-value' not in repr(rows)
    assert result.values['LLM_DEFAULT_MODEL'] == 'application-model'


def test_profile_review_reloads_changed_declaration_and_inherited_source(tmp_path):
    tests, result, schema = configured(tmp_path)
    first = profile_rows(tests, result, schema, ROOT)
    path = tmp_path / 'provider/test-profiles.toml'
    path.write_text(path.read_text().replace('ordinary-model', 'updated-model'))
    second = profile_rows(tests, result, schema, ROOT)
    assert any(cells[2] == 'ordinary-model' for _, cells in first)
    assert any(cells[2] == 'updated-model' for _, cells in second)
    assert any(cells[3].startswith('application:') for _, cells in second)


def test_picker_cost_context_uses_each_effective_profile_and_groups_identical_cases(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from api_agent.tui.test_configuration import profile_cost_configs
    tests, result, schema = configured(tmp_path)
    duplicate = TestIdentity('base:another', 'base', tests[0].file, tests[0].node_id, root=ROOT)
    fingerprint = Mock(side_effect=lambda context, values: dict(values, fingerprint=values['LLM_DEFAULT_MODEL']))
    monkeypatch.setattr('api_agent.testing.cost_context.cost_config', fingerprint)
    configs = profile_cost_configs((*tests, duplicate), result, SimpleNamespace(owner_root=ROOT))
    assert configs[tests[0].id]['fingerprint'] == 'ordinary-model'
    assert configs[tests[1].id]['fingerprint'] == 'workflow-model'
    assert configs[duplicate.id] == configs[tests[0].id]
    assert fingerprint.call_count == 2
    assert result.values['LLM_DEFAULT_MODEL'] == 'application-model'


def test_selected_review_tracks_profile_changes_without_importing_factory(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from api_agent.tui.test_configuration import selected_review
    tests, result, schema = configured(tmp_path)
    snapshot = SimpleNamespace(execution=result, schema=schema, sources_current=lambda: True)
    session = SimpleNamespace(context=SimpleNamespace(owner_root=ROOT),
        select_operation=Mock(), review_snapshot=lambda: snapshot)
    review = selected_review(session, tests)
    session.select_operation.assert_called_once_with(tests)
    assert review.sources_current()
    path = tmp_path / 'provider/test-profiles.toml'
    path.write_text(path.read_text().replace('ordinary-model', 'new-model'))
    assert not review.sources_current()


def test_selected_review_without_provider_and_changed_configuration(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from api_agent.tui.test_configuration import selected_review
    from unittest.mock import Mock
    import pytest
    tests, result, schema = configured(tmp_path)
    result.values.pop('LLM_PROVIDER')
    snapshot = SimpleNamespace(execution=result, schema=schema, sources_current=Mock(return_value=True))
    session = SimpleNamespace(context=SimpleNamespace(owner_root=ROOT), review_snapshot=lambda: snapshot)
    review = selected_review(session, tests)
    assert review.source is None and review.sources_current()
    snapshot.sources_current.return_value = False
    assert not review.sources_current()
    with pytest.raises(ValueError, match='sources changed'):
        selected_review(session, tests)


@pytest.mark.asyncio
async def test_selected_review_mounts_case_setting_value_and_source_columns(tmp_path):
    from textual.widgets import DataTable
    from api_agent.testing.selection import SelectionModel
    from test_tui import fixture
    app = fixture(tmp_path, fields={'LLM_DEFAULT_MODEL': {
        'type': 'str', 'default': 'example-model', 'acknowledge_default': False}})
    test = TestIdentity('base:case', 'base', 'test_case.py', 'test_case.py::test_case')
    async with app.run_test(size=(110, 35)) as pilot:
        await pilot.pause()
        app.mode = 'integration'
        app.selection = SelectionModel((test,))
        app.show_selection()
        await pilot.pause()
        assert ('review-config', 'Review selected-test configuration') in app.screen.actions
        await app.choose('review-config')
        await pilot.pause()
        assert app.screen.kind == 'case-configuration'
        table = app.screen.query_one(DataTable)
        assert [str(column.label) for column in table.columns.values()] == [
            'Test', 'Setting', 'Effective value', 'Source']
        assert table.row_count == 1
        assert list(table.get_row_at(0)) == [test.id, 'LLM_DEFAULT_MODEL',
            'example-model', 'application:shared_default']


@pytest.mark.parametrize('provider', ['gpt', 'bedrock'])
def test_provider_declarations_hide_test_controls_from_ordinary_review(provider):
    from api_agent.testing.test_profiles import TEST_KEYS
    location = ROOT / f'src/api_agent/llm_providers/{provider}/factory.py'
    schema = bootstrap_schema(load_shared_schema(), ['--help'], {}, {},
        {'LLM_PROVIDER': str(location) + ':factory'}, owner_root=ROOT)
    assert TEST_KEYS <= schema.fields.keys()
    assert all(not schema.fields[name].acknowledge_default for name in TEST_KEYS)
    assert schema.fields['LLM_DEFAULT_MODEL'].acknowledge_default


@pytest.mark.parametrize('saved_false', [False, True])
def test_bedrock_reasoning_review_reports_profile_or_explicit_dotenv_override(saved_false):
    from api_agent.testing.reasoning_contrast import scope
    from api_agent.testing.test_profiles import resolve_test_profiles
    location = ROOT / 'src/api_agent/llm_providers/bedrock/factory.py'
    overrides = {'TUI_MODE': 'integration', 'LLM_PROVIDER': str(location) + ':factory'}
    schema = bootstrap_schema(load_shared_schema(), ['--help'], {}, {}, overrides, owner_root=ROOT)
    dotenv = {'LLM_REASONING_TEST_TOKEN_REPORTING': 'False'} if saved_false else {}
    result = resolve_config(schema, environ={}, dotenv=dotenv, overrides=overrides)
    test = TestIdentity('base:reasoning', 'base', 'test_reasoning_contrast.py',
        'tests/integration/test_reasoning_contrast.py::test_reasoning_contrast', root=ROOT)
    before = dict(result.values)
    assert result.values['LLM_REASONING_TEST_MODEL'] == ''
    rows = {cells[1]: cells[2:] for _, cells in profile_rows((test,), result, schema, ROOT)}
    effective = resolve_test_profiles((test,), result.values, owner_root=ROOT, fields=result.fields)[test.id]['config']
    assert rows['LLM_REASONING_TEST_MODEL'][0] == 'openai.gpt-oss-120b'
    assert rows['LLM_REASONING_TEST_HIGH_EFFORT'][0] == 'medium'
    assert rows['LLM_REASONING_TEST_MAX_OUTPUT_TOKENS'][0] == '16000'
    assert rows['LLM_SETUP_REQUEST_TIMEOUT_SECONDS'][0] == '960.0'
    if saved_false:
        assert rows['LLM_REASONING_TEST_TOKEN_REPORTING'] == ('False', 'dotenv')
        with pytest.raises(ValueError, match='qualified for reasoning-token reporting'):
            scope(effective)
    else:
        assert rows['LLM_REASONING_TEST_TOKEN_REPORTING'] == (
            'True', 'provider_test_profile:test-profiles.toml:reasoning')
        assert scope(effective)
    assert result.values == before
    assert dotenv == ({'LLM_REASONING_TEST_TOKEN_REPORTING': 'False'} if saved_false else {})


@pytest.mark.parametrize('provider', ['gpt', 'bedrock'])
def test_ordinary_review_excludes_hidden_test_controls_even_when_explicit(tmp_path, provider):
    from types import SimpleNamespace
    from api_agent.testing.test_profiles import TEST_KEYS
    from test_tui import fixture
    location = ROOT / f'src/api_agent/llm_providers/{provider}/factory.py'
    overrides = {'TUI_MODE': 'integration', 'LLM_PROVIDER': str(location) + ':factory'}
    schema = bootstrap_schema(load_shared_schema(), ['--help'], {}, {}, overrides, owner_root=ROOT)
    result = resolve_config(schema, environ={},
        dotenv={'LLM_REASONING_TEST_TOKEN_REPORTING': 'False'}, overrides=overrides)
    app = fixture(tmp_path)
    app.schema = schema
    app.session = SimpleNamespace(result=lambda **kwargs: result,
        runtime_required_fields=(), review_field_names=lambda: frozenset(schema.fields))
    app.review_names = set(schema.fields)
    assert not TEST_KEYS & app.configuration_pending()
    names = {name for name, _ in app.configuration_rows(review=result)}
    assert not TEST_KEYS & names
    assert 'LLM_DEFAULT_MODEL' in names


def test_selected_review_passes_collection_context_for_implicit_base_cases(tmp_path):
    from dataclasses import replace
    from types import SimpleNamespace
    from test_test_profiles import configuration, CASES
    from api_agent.tui.test_configuration import selected_review
    from api_agent.testing.collection import repository_label
    context = SimpleNamespace(base_root=ROOT, owner_root=tmp_path)
    result = configuration()
    schema = bootstrap_schema(load_shared_schema(), ['--help'], {}, {}, result.values, owner_root=ROOT)
    tests = [replace(case, repo=repository_label(context, ROOT), root=None) for case in (CASES[0], CASES[-1])]
    snapshot = SimpleNamespace(execution=result, schema=schema, sources_current=lambda: True)
    session = SimpleNamespace(context=context, review_snapshot=lambda: snapshot)
    rows = {cells[:2]: cells[2:] for _, cells in selected_review(session, tests).rows}
    key = 'LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'
    assert rows[(tests[0].id, key)][1] == 'inactive_bundled_test_control:shared_default'
    assert rows[(tests[1].id, key)][0] == '1024'
    assert rows[(tests[1].id, key)][1].endswith(':workflow')
